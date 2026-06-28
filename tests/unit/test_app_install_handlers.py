"""
All-OS app-installation VERIFICATION suite for the unified AppInstaller.

This is the behavioural counterpart to the NixOS VM test
``nixos/tests/hart-app-install-verify.nix``: it boots the REAL installer
in-process, mocks ONLY the subprocess / filesystem boundary, calls the REAL
handler, and asserts the observable outcome for EVERY platform the installer
supports (Flatpak, AppImage, Nix, Wine/Windows, Waydroid/Android, plus the
browser-extension, snap and .hartpkg surfaces).

Scope boundary (DRY - this file deliberately does NOT re-test what the two
sibling suites already cover):
  * ``tests/unit/test_app_installer.py`` covers per-handler success/failure +
    the Flask routes.
  * ``tests/unit/test_app_installer_per_type.py`` covers the REAL/STAGED/
    HONEST-UNSUPPORTED honesty contract per type + the symmetric uninstall.

What THIS file adds, framed around the one signal both siblings leave
unasserted - the POSITIVE RUNTIME CONFIRMATION (``InstallResult.verified``):
  1. The ``verified`` truth table (success and not-staged) directly.
  2. A consolidated "all five platforms reach their confirmation step" sweep
     that asserts ``.verified is True`` (NOT merely ``.success``) for each.
  3. The mirror missing-tool sweep: every platform fails GRACEFULLY (returns an
     honest InstallResult, never raises) and is NOT verified.
  4. The full ``_EXT_PLATFORM_MAP`` table drives ``detect_platform`` (covers
     ``.xapk`` / ``.aab`` / ``.bat`` that the siblings omit).
  5. The background-install job (``start_install`` -> ``get_progress``)
     propagates the handler's ``verified`` / ``staged`` outcome - an untested
     path.

Every test imports the real code, mocks the boundary, calls the real function,
and asserts observable behaviour. NO grep / source-shape assertions.

Run under pytest, or directly (pytest can OOM on the dev box):
    C:/Users/sathi/miniconda3/python.exe tests/unit/test_app_install_handlers.py
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time

from unittest.mock import patch, MagicMock

from integrations.agent_engine.app_installer import (
    AppInstaller, InstallRequest, InstallResult, InstallerPlatform,
    detect_platform, _EXT_PLATFORM_MAP,
)

_MOD = 'integrations.agent_engine.app_installer'


# ─── helpers ────────────────────────────────────────────────

@contextlib.contextmanager
def _installer():
    """A fresh AppInstaller with an isolated temp install dir (cleaned up)."""
    inst = AppInstaller()
    d = tempfile.mkdtemp(prefix='hart_appinstall_')
    inst._install_dir = d
    try:
        yield inst
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _tmpfile(suffix, magic=b'\x00'):
    """A real file on disk (handlers early-return 'File not found' otherwise)."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'wb') as f:
        f.write(magic + b'\x00' * 64)
    return path


def _ok(returncode=0, stdout='', stderr=''):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ═══════════════════════════════════════════════════════════════
# 1. InstallResult.verified - the positive-confirmation truth table
# ═══════════════════════════════════════════════════════════════

def test_verified_truth_table():
    """verified == success AND NOT staged. A staged file is never verified;
    a non-staged success IS the post-install verified signal."""
    assert InstallResult(success=True, platform='nix', name='x').verified is True
    # A confirmed, non-staged success is verified.
    assert InstallResult(
        success=True, staged=False, platform='nix', name='x').verified is True
    # Staged file on disk that no runtime confirmed -> NOT verified.
    assert InstallResult(
        success=False, staged=True, platform='android', name='x').verified is False
    # Defensive: even if a buggy caller set success+staged, staged wins.
    assert InstallResult(
        success=True, staged=True, platform='android', name='x').verified is False
    # Plain failure -> not verified.
    assert InstallResult(
        success=False, platform='nix', name='x', error='boom').verified is False


# ═══════════════════════════════════════════════════════════════
# 2. Confirmation sweep - each platform handler reaches its POSITIVE
#    runtime confirmation and the result is VERIFIED.
# ═══════════════════════════════════════════════════════════════

def test_nix_confirm_sets_verified():
    """nix-env exit 0 is the confirmation -> success + verified, with a timeout
    on the subprocess call (the standing subprocess rule)."""
    with _installer() as inst, patch(f'{_MOD}.subprocess.run') as run:
        run.return_value = _ok(0)
        res = inst._install_nix(InstallRequest(source='nixpkgs.hello'))
    assert res.success is True
    assert res.verified is True
    assert res.platform == 'nix'
    assert 'timeout' in run.call_args.kwargs


def test_flatpak_confirm_sets_verified():
    with _installer() as inst, patch(f'{_MOD}.subprocess.run') as run:
        run.return_value = _ok(0)
        res = inst._install_flatpak(InstallRequest(source='flathub:org.test.App'))
    assert res.success is True
    assert res.verified is True
    assert res.platform == 'flatpak'
    assert 'timeout' in run.call_args.kwargs


def test_appimage_confirm_is_file_on_disk():
    """AppImage confirmation = the file is present + executable in the install
    dir; that non-staged success is the verified signal."""
    src = _tmpfile('.AppImage', magic=b'\x7fELF')
    try:
        with _installer() as inst:
            res = inst._install_appimage(InstallRequest(source=src))
            assert res.success is True
            assert res.verified is True
            # The positive confirmation: the copied file actually exists.
            assert os.path.isfile(res.install_path)
    finally:
        os.unlink(src)


def test_windows_confirm_sets_verified():
    """Wine present + exit 0 -> verified (the weak-but-honest Wine contract)."""
    src = _tmpfile('.exe', magic=b'MZ')
    try:
        with _installer() as inst, \
                patch(f'{_MOD}.shutil.which', return_value='/usr/bin/wine64'), \
                patch(f'{_MOD}.subprocess.run', return_value=_ok(0)):
            res = inst._install_windows(InstallRequest(source=src))
        assert res.success is True
        assert res.verified is True
        assert res.platform == 'windows'
    finally:
        os.unlink(src)


def test_android_confirm_via_app_list_sets_verified():
    """Android confirmation = the parsed package id appears in
    `waydroid app list` after install; exit 0 alone is not enough. A confirmed
    install is verified, and every subprocess call carries a timeout."""
    src = _tmpfile('.apk', magic=b'PK')

    def fake_run(cmd, **kw):
        assert 'timeout' in kw, f'no timeout on waydroid call: {cmd}'
        if cmd[:3] == ['/usr/bin/waydroid', 'app', 'install']:
            return _ok(0)
        if cmd[:3] == ['/usr/bin/waydroid', 'app', 'list']:
            return _ok(0, stdout='com.hart.testapp\n')
        return _ok(0)

    try:
        with _installer() as inst, \
                patch.object(inst, '_apk_package_name',
                             return_value='com.hart.testapp'), \
                patch(f'{_MOD}.shutil.which', return_value='/usr/bin/waydroid'), \
                patch.object(inst, '_waydroid_session_live', return_value=True), \
                patch(f'{_MOD}.subprocess.run', side_effect=fake_run):
            res = inst._install_android(InstallRequest(source=src))
        assert res.success is True
        assert res.verified is True
        assert res.staged is False
        assert res.app_id == 'com.hart.testapp'
    finally:
        os.unlink(src)


def test_browser_ext_confirm_reads_policy_back_verified():
    """A .crx force-install confirms by reading the managed-policy file back off
    disk and asserting the id is present -> verified."""
    src = _tmpfile('.crx', magic=b'Cr24')
    policy_dir = tempfile.mkdtemp(prefix='hart_chromium_policy_')
    try:
        with _installer() as inst, \
                patch(f'{_MOD}.shutil.which',
                      side_effect=lambda b: '/usr/bin/chromium'
                      if b == 'chromium' else None):
            res = inst._install_browser_ext(InstallRequest(
                source=src,
                options={'id': 'abcdefghijklmnop', 'policy_dir': policy_dir}))
        assert res.success is True
        assert res.verified is True
        # The confirmation is on disk, not just in the return value.
        with open(res.install_path) as f:
            ids = [e.split(';')[0]
                   for e in json.load(f).get('ExtensionInstallForcelist', [])]
        assert 'abcdefghijklmnop' in ids
    finally:
        os.unlink(src)
        shutil.rmtree(policy_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
# 3. Missing-tool sweep - every platform fails GRACEFULLY (honest
#    InstallResult, never an exception) and is NOT verified.
# ═══════════════════════════════════════════════════════════════

def test_nix_missing_tool_graceful():
    with _installer() as inst, \
            patch(f'{_MOD}.subprocess.run', side_effect=FileNotFoundError):
        res = inst._install_nix(InstallRequest(source='nixpkgs.hello'))
    assert res.success is False
    assert res.verified is False
    assert 'not available' in res.error.lower()


def test_flatpak_missing_tool_graceful():
    with _installer() as inst, \
            patch(f'{_MOD}.subprocess.run', side_effect=FileNotFoundError):
        res = inst._install_flatpak(InstallRequest(source='flathub:org.test.App'))
    assert res.success is False
    assert res.verified is False
    assert 'not available' in res.error.lower()


def test_appimage_missing_source_graceful():
    """AppImage has no external tool; its boundary failure is a missing source.
    It must return an honest result, not raise."""
    with _installer() as inst:
        res = inst._install_appimage(
            InstallRequest(source='/no/such/file.AppImage'))
    assert res.success is False
    assert res.verified is False
    assert 'not found' in res.error.lower()


def test_windows_missing_wine_graceful():
    src = _tmpfile('.exe', magic=b'MZ')
    try:
        with _installer() as inst, \
                patch(f'{_MOD}.shutil.which', return_value=None):
            res = inst._install_windows(InstallRequest(source=src))
        assert res.success is False
        assert res.verified is False
        assert 'wine' in res.error.lower()
    finally:
        os.unlink(src)


def test_android_missing_runtime_stages_not_verified():
    """No Waydroid -> the APK is STAGED to disk (file present) but NOT installed:
    success=False, staged=True, and crucially verified=False (a staged file the
    runtime never confirmed is not a verified install)."""
    src = _tmpfile('.apk', magic=b'PK')
    try:
        with _installer() as inst, \
                patch.object(inst, '_apk_package_name',
                             return_value='com.hart.testapp'), \
                patch(f'{_MOD}.shutil.which', return_value=None):
            res = inst._install_android(InstallRequest(source=src))
        assert res.success is False
        assert res.staged is True
        assert res.verified is False
        assert 'hart.subsystems.android' in res.error
    finally:
        os.unlink(src)


def test_macos_missing_darling_graceful():
    """Bonus 6th platform: macOS with no Darling fails honestly, not verified."""
    with _installer() as inst, \
            patch(f'{_MOD}.shutil.which', return_value=None):
        res = inst._install_macos(InstallRequest(source='app.app'))
    assert res.success is False
    assert res.verified is False
    assert 'darling' in res.error.lower()


# ═══════════════════════════════════════════════════════════════
# 4. Confirmation TEETH - exit 0 without confirmation is NOT verified
# ═══════════════════════════════════════════════════════════════

def test_android_exit_zero_unconfirmed_is_not_verified():
    """`waydroid app install` returns 0 but the package is absent from
    `waydroid app list` -> success=False and verified=False. exit 0 is not
    proof; the app-list confirmation is."""
    src = _tmpfile('.apk', magic=b'PK')

    def fake_run(cmd, **kw):
        if cmd[:3] == ['/usr/bin/waydroid', 'app', 'install']:
            return _ok(0, stderr='E: silent failure')
        if cmd[:3] == ['/usr/bin/waydroid', 'app', 'list']:
            return _ok(0, stdout='(no apps)\n')
        return _ok(0)

    try:
        with _installer() as inst, \
                patch.object(inst, '_apk_package_name',
                             return_value='com.hart.testapp'), \
                patch(f'{_MOD}.shutil.which', return_value='/usr/bin/waydroid'), \
                patch.object(inst, '_waydroid_session_live', return_value=True), \
                patch(f'{_MOD}.subprocess.run', side_effect=fake_run):
            res = inst._install_android(InstallRequest(source=src))
        assert res.success is False
        assert res.verified is False
        assert 'did not register' in res.error
    finally:
        os.unlink(src)


def test_snap_honest_refusal_not_verified():
    """Snap is honestly unsupported: a real handler that refuses (not a crash,
    not a silent nix misroute) and is never verified."""
    with _installer() as inst:
        res = inst._install_snap(InstallRequest(source='snap:firefox'))
    assert res.success is False
    assert res.verified is False
    assert res.platform == 'snap'
    assert 'not supported' in res.error.lower()


# ═══════════════════════════════════════════════════════════════
# 5. Extension -> platform mapping table drives detection
# ═══════════════════════════════════════════════════════════════

def test_ext_platform_map_drives_detect_platform():
    """detect_platform agrees with EVERY entry in _EXT_PLATFORM_MAP (covers
    .xapk/.aab/.bat that the per-handler suites omit). Importing the real map
    means adding an extension without a detection path fails this test."""
    assert _EXT_PLATFORM_MAP, 'extension map is empty'
    for ext, expected in _EXT_PLATFORM_MAP.items():
        assert detect_platform('demo' + ext) == expected, \
            f'{ext} should map to {expected}'


def test_detect_unknown_extension_is_unknown():
    assert detect_platform('notes.txt') == InstallerPlatform.UNKNOWN
    assert detect_platform('binary_no_ext') == InstallerPlatform.UNKNOWN


def test_known_apk_variants_route_to_android():
    """The three Android packaging extensions all resolve to ANDROID so none
    falls through to the nix catch-all in install()."""
    for ext in ('.apk', '.xapk', '.aab'):
        assert detect_platform('game' + ext) == InstallerPlatform.ANDROID


# ═══════════════════════════════════════════════════════════════
# 6. install() dispatch propagates the verified / staged signal
# ═══════════════════════════════════════════════════════════════

def test_install_dispatch_propagates_verified():
    """A handler that returns a confirmed (non-staged) success surfaces
    verified=True through the top-level install() dispatch."""
    with _installer() as inst, \
            patch.object(inst, '_auto_register_app'), \
            patch.object(inst, '_install_flatpak') as h:
        h.return_value = InstallResult(
            success=True, staged=False, platform='flatpak', name='gimp',
            app_id='org.gimp.GIMP')
        res = inst.install(InstallRequest(
            source='flathub:org.gimp.GIMP',
            platform=InstallerPlatform.FLATPAK))
    assert res.success is True
    assert res.verified is True


def test_install_dispatch_propagates_staged_not_verified():
    """A staged android result flows through install() as not-verified."""
    with _installer() as inst, \
            patch.object(inst, '_install_android') as h:
        h.return_value = InstallResult(
            success=False, staged=True, platform='android', name='app',
            app_id='com.x.y', error='staged')
        res = inst.install(InstallRequest(
            source='/tmp/app.apk', platform=InstallerPlatform.ANDROID))
    assert res.success is False
    assert res.staged is True
    assert res.verified is False


# ═══════════════════════════════════════════════════════════════
# 7. Background install job propagates verified / staged / error
# ═══════════════════════════════════════════════════════════════

def _run_job_and_wait(inst, result):
    """Drive start_install with install() mocked to return ``result``; wait for
    the worker thread, return the final progress snapshot."""
    with patch.object(inst, 'install', return_value=result):
        env = inst.start_install(InstallRequest(source='x', name='x'))
        assert env['ok'] is True
        worker = inst._job_thread
        if worker is not None:
            worker.join(timeout=5)
    # The worker may set the terminal phase a hair after install() returns;
    # poll briefly for the terminal phase to avoid a thread-timing flake.
    deadline = time.time() + 2
    snap = inst.get_progress()
    while snap.get('phase') not in ('done', 'error') and time.time() < deadline:
        time.sleep(0.02)
        snap = inst.get_progress()
    return snap


def test_background_job_reports_verified():
    with _installer() as inst:
        snap = _run_job_and_wait(inst, InstallResult(
            success=True, staged=False, platform='nix', name='htop',
            app_id='htop', version='3.2'))
    assert snap['phase'] == 'done'
    assert snap['success'] is True
    assert snap['verified'] is True
    assert snap['staged'] is False


def test_background_job_reports_staged_not_verified():
    with _installer() as inst:
        snap = _run_job_and_wait(inst, InstallResult(
            success=False, staged=True, platform='android', name='app',
            app_id='com.x.y', error='staged'))
    assert snap['phase'] == 'done'
    assert snap['success'] is False
    assert snap['staged'] is True
    assert snap['verified'] is False


def test_background_job_reports_error_not_verified():
    with _installer() as inst:
        snap = _run_job_and_wait(inst, InstallResult(
            success=False, staged=False, platform='nix', name='bad',
            error='package not found'))
    assert snap['phase'] == 'error'
    assert snap['success'] is False
    assert snap['verified'] is False
    assert 'not found' in (snap.get('error') or '')


# ─── standalone runner (pytest OOMs on the dev box) ─────────

if __name__ == '__main__':
    import sys
    tests = sorted(
        (name, obj) for name, obj in list(globals().items())
        if name.startswith('test_') and callable(obj))
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except Exception as e:  # noqa: BLE001 - test harness reports all
            failures.append((name, e))
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'\n{len(tests) - len(failures)}/{len(tests)} passed')
    sys.exit(1 if failures else 0)
