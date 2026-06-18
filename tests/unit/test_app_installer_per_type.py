"""
Per-type HONESTY audit for the cross-platform AppInstaller.

Goal: for EVERY install type the unified installer dispatches to, prove
behaviourally — by mocking the subprocess / filesystem boundary, calling the
REAL handler, and asserting observable behaviour — whether it is:

  REAL  : constructs + invokes the correct install command with the right
          package/app argument AND propagates a genuine failure
          (boundary fails -> handler reports failure, not success).
  FAKE  : returns success WITHOUT doing the work, or claims success even when
          the boundary FAILS (the dangerous case the user must know about).
  STUB  : returns failure / not-implemented even when its tool is present.

These complement ``test_app_installer.py`` (which covers the happy paths and
detection). The tests HERE are specifically the fakeness / failure-propagation
proofs that the existing suite does NOT make:

  * Wine now PROPAGATES failure: a non-zero wine exit -> success=False with the
    error surfaced (app_installer.py:602-616, FIXED). rc=0 stays a best-effort
    success (wine's 0 is a weak signal for GUI installers).
  * Android's copy fallback reports success for a mere file copy when no adb is
    present (app_installer.py:646-654) — "installed" == "copied a file".
  * macOS returns success=False even WITH darling on PATH (the handler is a
    stub: app_installer.py:671-673).
  * REAL installers (nix/flatpak/appimage/extension) DO propagate a genuine
    boundary failure as success=False — the contrast that exposes Wine.
  * Snap is not a supported platform at all (no enum member, no handler).

NO grep / source-shape assertions: every test imports the real handler, mocks
the boundary, calls the real method, and asserts the returned InstallResult.

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
        # Real command, real argument (the package the caller asked for).
        self.assertEqual(cmd[0], 'nix-env')
        self.assertIn('-iA', cmd)
        self.assertIn('nixpkgs.htop', cmd)

    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_propagates_failure(self, mock_run):
        """Boundary FAILS (returncode=1) -> handler must report failure.
        This is the contract Wine VIOLATES."""
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
    """flatpak is REAL: real command, real exit-code check."""

    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_invokes_flatpak_install_with_ref(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        res = self.installer._install_flatpak(
            InstallRequest(source='flathub:org.gimp.GIMP'))
        self.assertTrue(res.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'flatpak')
        self.assertIn('install', cmd)
        self.assertIn('org.gimp.GIMP', cmd)  # ref, prefix stripped

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
    """appimage is REAL: it actually copies the file to the install dir and
    makes it executable; a copy failure is reported as failure."""

    def test_copies_file_into_install_dir(self):
        src = _tmpfile('.AppImage', magic=b'\x7fELF')
        try:
            res = self.installer._install_appimage(InstallRequest(source=src))
            self.assertTrue(res.success)
            # The work actually happened: the destination file exists on disk.
            self.assertTrue(os.path.isfile(res.install_path))
            self.assertTrue(res.install_path.endswith(os.path.basename(src)))
        finally:
            os.unlink(src)

    def test_propagates_copy_failure(self):
        """A real boundary failure (shutil.copy2 raises) -> success=False."""
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
# FAKE — Wine: returns success=True even when the wine subprocess FAILS
# ═══════════════════════════════════════════════════════════════════════════

class TestWindowsWineFailurePropagation(_InstallerCase):
    """Wine now HONOURS the failure signal (fixed): _install_windows checks
    result.returncode — a non-zero wine exit -> success=False with the error
    surfaced, while rc=0 stays a best-effort success (wine's 0 is a weak signal
    for GUI installers, but a non-zero exit is a reliable failure).

    Regression guard for the FIXED bug: this used to return success=True
    UNCONDITIONALLY after subprocess.run, so a failed install was reported as a
    success and got a desktop icon that launches nothing.
    """

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/wine64')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_propagates_failure_when_wine_exits_nonzero(self, mock_run, _which):
        # Wine FAILS hard: returncode=1, error on stderr.
        mock_run.return_value = MagicMock(
            returncode=1, stderr='wine: cannot find MZ', stdout='')
        src = _tmpfile('.exe', magic=b'MZ')
        try:
            res = self.installer._install_windows(InstallRequest(source=src))
            # FIXED: a non-zero wine exit is a reliable failure -> success=False.
            self.assertFalse(
                res.success,
                "Wine must report failure on rc!=0 (the fixed bug: it used to "
                "claim success unconditionally).")
            # And the wine exit/stderr is surfaced, not swallowed.
            self.assertIn('exited 1', res.error)
            self.assertEqual(res.platform, 'windows')
        finally:
            os.unlink(src)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/wine64')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_does_invoke_wine_with_the_exe(self, mock_run, _which):
        """It DOES build+run a real wine command (so the fakeness is in the
        result-handling, not in skipping the boundary entirely)."""
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
        """The ONE honest failure path: no wine binary on PATH -> failure."""
        src = _tmpfile('.exe', magic=b'MZ')
        try:
            res = self.installer._install_windows(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('Wine', res.error)
        finally:
            os.unlink(src)


# ═══════════════════════════════════════════════════════════════════════════
# PARTIAL/MISLEADING — Android: adb path real, copy fallback reports success
# for a plain file copy (and the runtime is inert: `sleep infinity` in NixOS)
# ═══════════════════════════════════════════════════════════════════════════

class TestAndroidIsMisleading(_InstallerCase):
    """Android is PARTIAL/MISLEADING.

    * The adb branch IS real (checks returncode==0).
    * BUT when no adb is present (the actual on-device situation — the NixOS
      android runtime is `exec sleep infinity`, hart-subsystems.nix:288, i.e.
      no ART/Waydroid), the handler falls back to a plain ``shutil.copy2`` and
      returns success=True. Copying an .apk into a directory is NOT installing
      it; nothing can ever run it. "installed" here means "file copied".
    """

    def _binder_present(self):
        """Patch os.path.exists so /dev/binder looks present, real paths
        pass through."""
        real_exists = os.path.exists

        def fake(p):
            if p == '/dev/binder':
                return True
            return real_exists(p)
        return patch('integrations.agent_engine.app_installer.os.path.exists',
                     side_effect=fake)

    @patch('integrations.agent_engine.app_installer.shutil.which')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_adb_branch_is_real(self, mock_run, mock_which):
        """With adb present and rc=0, it invokes `adb install -r <apk>`."""
        mock_which.return_value = '/usr/bin/adb'
        mock_run.return_value = MagicMock(returncode=0, stderr='', stdout='Success')
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with self._binder_present():
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertTrue(res.success)
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], '/usr/bin/adb')
            self.assertIn('install', cmd)
            self.assertIn(src, cmd)
        finally:
            os.unlink(src)

    @patch('integrations.agent_engine.app_installer.shutil.which')
    @patch('integrations.agent_engine.app_installer.subprocess.run')
    def test_adb_failure_falls_through_to_copy_and_still_succeeds(
            self, mock_run, mock_which):
        """Even if adb FAILS (rc=1), the handler does not report failure — it
        silently falls through to the copy fallback and returns success. So an
        adb-level install error is masked as success."""
        mock_which.return_value = '/usr/bin/adb'
        mock_run.return_value = MagicMock(
            returncode=1, stderr='INSTALL_FAILED_INVALID_APK', stdout='')
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with self._binder_present():
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertTrue(res.success)        # masked failure
            self.assertTrue(os.path.isfile(res.install_path))  # just a copy
        finally:
            os.unlink(src)

    def test_copy_fallback_reports_success_for_a_mere_copy(self):
        """No adb on PATH + binder present -> the handler ONLY copies the file
        and calls that an install. Prove the boundary that was hit is the
        filesystem copy, not any package-install command."""
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with self._binder_present():
                with patch('integrations.agent_engine.app_installer.shutil.which',
                           return_value=None):  # no adb
                    # subprocess.run must NOT be the thing that "installs":
                    # if it is called at all it would only be adb, which is
                    # absent, so guard that no install command runs.
                    with patch('integrations.agent_engine.app_installer.'
                               'subprocess.run',
                               side_effect=AssertionError(
                                   'no package-install command should run in '
                                   'the copy fallback')):
                        res = self.installer._install_android(
                            InstallRequest(source=src))
            self.assertTrue(res.success)
            # The ONLY observable effect is a copied file — not an install.
            self.assertTrue(os.path.isfile(res.install_path))
            self.assertEqual(
                os.path.basename(res.install_path), os.path.basename(src))
        finally:
            os.unlink(src)

    def test_no_binder_fails_cleanly(self):
        """The one honest failure: no /dev/binder -> failure (subsystem off)."""
        src = _tmpfile('.apk', magic=b'PK')
        try:
            with patch('integrations.agent_engine.app_installer.os.path.exists',
                       return_value=False):
                res = self.installer._install_android(InstallRequest(source=src))
            self.assertFalse(res.success)
            self.assertIn('Android subsystem', res.error)
        finally:
            os.unlink(src)


# ═══════════════════════════════════════════════════════════════════════════
# STUB — macOS: returns failure even WITH darling present
# ═══════════════════════════════════════════════════════════════════════════

class TestMacOSIsStub(_InstallerCase):
    """macOS is a STUB: app_installer.py:671-673 returns success=False even
    when darling is on PATH, and never invokes any install boundary."""

    @patch('integrations.agent_engine.app_installer.subprocess.run',
           side_effect=AssertionError('macOS stub must not run any subprocess'))
    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value='/usr/bin/darling')
    def test_with_darling_still_not_implemented(self, _which, _run):
        res = self.installer._install_macos(InstallRequest(source='app.dmg'))
        self.assertFalse(res.success)             # stub
        self.assertIn('not yet automated', res.error)

    @patch('integrations.agent_engine.app_installer.shutil.which',
           return_value=None)
    def test_without_darling_reports_absent(self, _which):
        res = self.installer._install_macos(InstallRequest(source='app.dmg'))
        self.assertFalse(res.success)
        self.assertIn('Darling', res.error)


# ═══════════════════════════════════════════════════════════════════════════
# REAL (in-process) — extension (.hartpkg): loads via the extension registry
# ═══════════════════════════════════════════════════════════════════════════

class TestExtensionIsReal(_InstallerCase):
    """The HART extension installer is REAL (in-process): it calls the
    extension registry's load() and reports failure when that raises or the
    registry is unavailable. NOTE: `.hartpkg` is HART's OWN extension format —
    it is NOT a browser `.crx`/`.xpi`; there is no real browser-extension
    install path anywhere in this installer."""

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


# ═══════════════════════════════════════════════════════════════════════════
# NOT SUPPORTED — Snap (no enum member, no handler, no dispatch)
# ═══════════════════════════════════════════════════════════════════════════

class TestSnapNotSupported(_InstallerCase):
    """Snap is NOT a supported platform: there is no InstallerPlatform.SNAP and
    no handler. Prove it behaviourally: a 'snap:'-prefixed source is NOT routed
    to any snap install; it is mis-detected as a nix package name (the
    catch-all). The user must know snap is unsupported, not silently nix'd."""

    def test_no_snap_enum_member(self):
        self.assertFalse(
            any(p.value == 'snap' for p in InstallerPlatform),
            "An InstallerPlatform.SNAP appeared — snap may now be supported; "
            "update the audit.")

    def test_snap_source_is_misrouted_to_nix_not_snap(self):
        """`snap install` is never invoked; the source falls through to the nix
        handler (catch-all). Behavioural proof via which() of the dispatch."""
        called = {}

        def fake_nix(req):
            called['nix'] = req.source
            return InstallResult(success=False, platform='nix', name=req.source,
                                 error='nix-env not available')

        with patch.object(self.installer, '_install_nix', side_effect=fake_nix):
            # 'snap:firefox' is not a file and has no nix:/flatpak: prefix, so
            # install() routes it to NIX (the unknown-string catch-all).
            res = self.installer.install(InstallRequest(source='snap:firefox'))
        self.assertIn('nix', called)                 # routed to nix, not snap
        self.assertEqual(res.platform, 'nix')
        self.assertFalse(res.success)


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch coverage — every declared platform maps to a handler (no orphan)
# ═══════════════════════════════════════════════════════════════════════════

class TestDispatchCoverage(_InstallerCase):
    """Every non-UNKNOWN/non-NIX-default platform reaches a distinct handler.
    (NIX is also the catch-all; this just asserts the table is wired.)"""

    def test_each_platform_routes_to_its_handler(self):
        mapping = {
            InstallerPlatform.NIX: '_install_nix',
            InstallerPlatform.FLATPAK: '_install_flatpak',
            InstallerPlatform.APPIMAGE: '_install_appimage',
            InstallerPlatform.WINDOWS: '_install_windows',
            InstallerPlatform.ANDROID: '_install_android',
            InstallerPlatform.MACOS: '_install_macos',
            InstallerPlatform.EXTENSION: '_install_extension',
        }
        for plat, handler_name in mapping.items():
            with patch.object(self.installer, handler_name) as mock_h:
                mock_h.return_value = InstallResult(
                    success=True, platform=plat.value, name='x')
                self.installer.install(
                    InstallRequest(source='x', platform=plat))
                mock_h.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
