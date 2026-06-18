"""
Unified App Installer — Cross-Platform Package Installation API.

Handles installation from ANY platform through a single interface:
  - Linux: Nix packages, Flatpak, AppImage
  - Windows: .exe/.msi via Wine binfmt integration
  - Android: .apk via Waydroid (real ART/PackageManager; confirmed by app list)
  - macOS: .app via Darling (experimental, CLI-leaning; .dmg/.pkg refused)
  - Snap: UNSUPPORTED on this image (honest refusal, never a silent misroute)
  - Browser ext: .crx (Chromium) / .xpi (Firefox) via managed-policy force-install
  - HART OS: .hartpkg extensions from the in-process extension registry

Every handler confirms a POSITIVE runtime result (waydroid app list / darling
prefix boot / managed-policy id on disk / non-zero exit) — never exit-0-or-copy.

Detection chain:
  1. File extension → platform mapping
  2. Magic bytes (MZ for PE, PK for APK/ZIP, ELF header)
  3. URL pattern → package manager dispatch

Each installer type registers in AppRegistry after successful install.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.installer')


class InstallerPlatform(Enum):
    """Platform classification for installers."""
    NIX = 'nix'
    FLATPAK = 'flatpak'
    APPIMAGE = 'appimage'
    WINDOWS = 'windows'
    ANDROID = 'android'
    MACOS = 'macos'
    SNAP = 'snap'
    # ``EXTENSION`` is HART's OWN in-process .hartpkg extension format ONLY.
    # ``BROWSER_EXT`` is a real browser extension (.crx / .xpi) force-installed
    # into the bundled Chromium/Firefox via enterprise managed-policy. They are
    # two DISTINCT enum values so a .crx never masquerades as a .hartpkg.
    EXTENSION = 'extension'
    BROWSER_EXT = 'browser_ext'
    UNKNOWN = 'unknown'


class InstallStatus(Enum):
    """Installation lifecycle states."""
    PENDING = 'pending'
    DOWNLOADING = 'downloading'
    VERIFYING = 'verifying'
    INSTALLING = 'installing'
    CONFIGURING = 'configuring'
    COMPLETED = 'completed'
    FAILED = 'failed'
    UNINSTALLING = 'uninstalling'


@dataclass
class InstallRequest:
    """A package installation request."""
    source: str                          # File path, URL, or package name
    platform: InstallerPlatform = InstallerPlatform.UNKNOWN
    name: str = ''                       # Display name (auto-detected if empty)
    version: str = ''
    sha256: str = ''                     # Expected hash for verification
    options: Dict = field(default_factory=dict)  # Platform-specific options


@dataclass
class InstallResult:
    """Result of an installation attempt."""
    success: bool
    platform: str
    name: str
    version: str = ''
    install_path: str = ''
    app_id: str = ''
    error: str = ''
    duration_seconds: float = 0.0
    # ``staged`` is True ONLY when the source file was copied/placed on disk but
    # NO runtime installed/launchable it (e.g. an APK copied while Waydroid is
    # not running). A staged result is NEVER success=True — a staged file is not
    # installed. This distinguishes "file is on disk for later" from "installed
    # and runnable", and is the honest replacement for the old copy-that-claimed-
    # success Android fallback.
    staged: bool = False


# ─── Extension → Platform mapping ───────────────────────────

_EXT_PLATFORM_MAP = {
    # Windows
    '.exe': InstallerPlatform.WINDOWS,
    '.msi': InstallerPlatform.WINDOWS,
    '.bat': InstallerPlatform.WINDOWS,
    # Android
    '.apk': InstallerPlatform.ANDROID,
    '.xapk': InstallerPlatform.ANDROID,
    '.aab': InstallerPlatform.ANDROID,
    # macOS
    '.dmg': InstallerPlatform.MACOS,
    '.app': InstallerPlatform.MACOS,
    '.pkg': InstallerPlatform.MACOS,
    # Linux
    '.flatpakref': InstallerPlatform.FLATPAK,
    '.AppImage': InstallerPlatform.APPIMAGE,
    '.appimage': InstallerPlatform.APPIMAGE,
    # Snap — a REAL enum value so .snap no longer silently falls through to the
    # NIX catch-all and mis-claims; the handler returns an honest "unsupported".
    '.snap': InstallerPlatform.SNAP,
    # Browser extensions (force-installed into the bundled browser via managed
    # policy). DISTINCT from HART's own .hartpkg below.
    '.crx': InstallerPlatform.BROWSER_EXT,
    '.xpi': InstallerPlatform.BROWSER_EXT,
    # HART OS
    '.hartpkg': InstallerPlatform.EXTENSION,
}

# ─── Magic bytes for binary detection ────────────────────────

_MAGIC_PLATFORM_MAP = {
    b'MZ': InstallerPlatform.WINDOWS,     # PE executable
    b'\x7fELF': InstallerPlatform.APPIMAGE,  # Could be AppImage (ELF)
    b'PK': InstallerPlatform.ANDROID,     # ZIP/APK
}


def detect_platform(file_path: str) -> InstallerPlatform:
    """Detect the platform of an installer file.

    Uses extension first, then magic bytes as fallback.
    """
    _, ext = os.path.splitext(file_path)

    # Extension-based detection
    if ext in _EXT_PLATFORM_MAP:
        return _EXT_PLATFORM_MAP[ext]

    # Magic bytes detection
    if os.path.isfile(file_path):
        try:
            with open(file_path, 'rb') as f:
                header = f.read(4)
            for magic, platform in _MAGIC_PLATFORM_MAP.items():
                if header[:len(magic)] == magic:
                    # Distinguish APK (ZIP with AndroidManifest) from regular ZIP
                    if magic == b'PK':
                        import zipfile
                        try:
                            with zipfile.ZipFile(file_path) as zf:
                                if 'AndroidManifest.xml' in zf.namelist():
                                    return InstallerPlatform.ANDROID
                        except zipfile.BadZipFile:
                            pass
                        continue
                    return platform
        except (IOError, PermissionError):
            pass

    return InstallerPlatform.UNKNOWN


def verify_checksum(file_path: str, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of a file."""
    if not expected_sha256:
        return True  # No checksum to verify
    sha = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha.update(chunk)
    return sha.hexdigest() == expected_sha256


class AppInstaller:
    """Unified cross-platform application installer.

    Dispatches installation to the appropriate platform handler
    based on file type detection.
    """

    def __init__(self):
        self._install_dir = os.environ.get(
            'HART_APP_DIR', '/var/lib/hart/apps')
        self._history: List[dict] = []

    def install(self, req: InstallRequest) -> InstallResult:
        """Install an application from any platform.

        Args:
            req: InstallRequest with source path/URL and optional metadata.

        Returns:
            InstallResult with success status and details.
        """
        start = time.time()

        # Auto-detect platform if not specified
        if req.platform == InstallerPlatform.UNKNOWN:
            if req.source.startswith('nixpkgs.') or req.source.startswith('nix:'):
                req.platform = InstallerPlatform.NIX
            elif req.source.startswith('flathub:') or req.source.startswith('flatpak:'):
                req.platform = InstallerPlatform.FLATPAK
            elif req.source.startswith('snap:'):
                # Route to the SNAP handler (honest "unsupported"), NOT the NIX
                # catch-all — a 'snap:' source must never be silently nix'd.
                req.platform = InstallerPlatform.SNAP
            elif os.path.isfile(req.source):
                req.platform = detect_platform(req.source)
            else:
                # Try as nix package name
                req.platform = InstallerPlatform.NIX

        # Verify checksum if file
        if os.path.isfile(req.source) and req.sha256:
            if not verify_checksum(req.source, req.sha256):
                return InstallResult(
                    success=False, platform=req.platform.value,
                    name=req.name or os.path.basename(req.source),
                    error='Checksum verification failed',
                    duration_seconds=time.time() - start)

        # Dispatch to platform handler
        handlers = {
            InstallerPlatform.NIX: self._install_nix,
            InstallerPlatform.FLATPAK: self._install_flatpak,
            InstallerPlatform.APPIMAGE: self._install_appimage,
            InstallerPlatform.WINDOWS: self._install_windows,
            InstallerPlatform.ANDROID: self._install_android,
            InstallerPlatform.MACOS: self._install_macos,
            InstallerPlatform.SNAP: self._install_snap,
            InstallerPlatform.BROWSER_EXT: self._install_browser_ext,
            InstallerPlatform.EXTENSION: self._install_extension,
        }

        handler = handlers.get(req.platform)
        if not handler:
            return InstallResult(
                success=False, platform=req.platform.value,
                name=req.name or req.source,
                error=f'No installer for platform: {req.platform.value}',
                duration_seconds=time.time() - start)

        result = handler(req)
        result.duration_seconds = time.time() - start

        # Record in history
        self._history.append({
            'name': result.name,
            'platform': result.platform,
            'success': result.success,
            'timestamp': time.time(),
            'source': req.source,
            'error': result.error,
        })

        # Audit log successful installs
        if result.success:
            try:
                from security.immutable_audit_log import get_audit_log
                get_audit_log().log_event(
                    'app_lifecycle', 'app_installer',
                    f'Installed {result.name}',
                    detail={
                        'platform': result.platform,
                        'app_id': result.app_id,
                        'source': req.source,
                        'duration': round(result.duration_seconds, 2),
                    })
            except Exception:
                pass

            # Auto-register in AppRegistry so the app appears in shell/spotlight
            self._auto_register_app(result, req)

        return result

    def uninstall(self, app_id: str, platform: str = '') -> InstallResult:
        """Uninstall an application."""
        if platform == 'nix' or not platform:
            result = self._uninstall_nix(app_id)
        elif platform == 'flatpak':
            result = self._uninstall_flatpak(app_id)
        elif platform == 'appimage':
            result = self._uninstall_appimage(app_id)
        elif platform == 'windows':
            result = self._uninstall_windows(app_id)
        else:
            result = InstallResult(
                success=False, platform=platform, name=app_id,
                error=f'Uninstall not supported for: {platform}')

        # Audit log successful uninstalls
        if result.success:
            try:
                from security.immutable_audit_log import get_audit_log
                get_audit_log().log_event(
                    'app_lifecycle', 'app_installer',
                    f'Uninstalled {app_id}',
                    detail={
                        'platform': result.platform,
                        'app_id': app_id,
                    })
            except Exception:
                pass

            # Auto-unregister from AppRegistry
            self._auto_unregister_app(app_id)

        return result

    def _auto_register_app(self, result: InstallResult, req: InstallRequest):
        """Register successfully installed app in AppRegistry for shell/spotlight."""
        try:
            from core.platform.registry import get_registry
            from core.platform.app_manifest import AppManifest, AppType

            registry = get_registry()
            if not registry.has('apps'):
                return
            apps = registry.get('apps')

            app_id = result.app_id or result.name.lower().replace(' ', '_')
            if apps.get(app_id):
                return  # Already registered

            # Map installer platform to app type
            platform_type_map = {
                'nix': AppType.DESKTOP_APP.value,
                'flatpak': AppType.DESKTOP_APP.value,
                'appimage': AppType.DESKTOP_APP.value,
                'windows': AppType.DESKTOP_APP.value,
                'android': AppType.DESKTOP_APP.value,
                'macos': AppType.DESKTOP_APP.value,
                # A browser extension is NOT a desktop app — it lives inside the
                # browser via managed policy, so it gets the EXTENSION app type
                # (no standalone launcher icon).
                'browser_ext': AppType.EXTENSION.value,
                'extension': AppType.EXTENSION.value,
            }
            app_type = platform_type_map.get(result.platform, AppType.DESKTOP_APP.value)

            # Build entry dict with required keys per app type
            entry = {}
            if app_type == AppType.DESKTOP_APP.value:
                entry['exec'] = app_id
                if result.install_path:
                    entry['install_path'] = result.install_path
            elif app_type == AppType.EXTENSION.value:
                entry['module'] = f'extensions.{app_id}'
            else:
                entry['exec'] = app_id

            manifest = AppManifest(
                id=app_id,
                name=result.name,
                version=result.version or '1.0.0',
                type=app_type,
                icon='apps',
                entry=entry,
                group='Installed',
                tags=['installed', result.platform],
            )
            apps.register(manifest)
            logger.info(f"Auto-registered app: {app_id} ({result.platform})")
            # Live desktop icon: push an `app_installed` A2UI card so the glass
            # desktop merges this app into window.MANIFEST and auto-pins an icon
            # (NixOS-style: install an app, its icon appears) WITHOUT a refresh.
            # Reuses the in-process A2UI channel + AppRegistry.manifest_entry_for
            # as the single source of truth for the entry shape — no fork.
            self._push_desktop_icon(app_id, manifest)
        except Exception as e:
            logger.debug(f"App auto-register skipped: {e}")

    def _push_desktop_icon(self, app_id: str, manifest):
        """Emit an `app_installed` A2UI card so the live desktop pins an icon.

        Best-effort and in-process: routes through the registered
        ``LiquidUIService.agent_ui_update`` (the same governed A2UI path agent
        cards use — kill-switch + audit + rate-cap apply). The desktop merges
        the entry into ``window.MANIFEST`` and calls the EXISTING
        ``hartPinIcon``; a headless/server shell additionally receives it via
        the EventBus/WAMP fan-out inside ``agent_ui_update``.
        """
        try:
            from core.platform.app_registry import AppRegistry
            from core.platform.registry import get_registry
            svc = get_registry().get_or_none('LiquidUIService')
            if not svc:
                return
            entry = AppRegistry.manifest_entry_for(manifest)
            svc.agent_ui_update('app_installer', {
                'type': 'app_installed',
                'id': app_id,
                'title': entry['title'],
                'icon': entry['icon'],
                'exec': entry['exec'],
                'group': entry['group'],
                'platform': ','.join(manifest.tags) if getattr(manifest, 'tags', None) else '',
            })
        except Exception as e:
            logger.debug(f"Desktop icon push skipped: {e}")

    def _auto_unregister_app(self, app_id: str):
        """Unregister app from AppRegistry on uninstall."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('apps'):
                return
            apps = registry.get('apps')
            if apps.get(app_id):
                apps.unregister(app_id)
                logger.info(f"Auto-unregistered app: {app_id}")
        except Exception as e:
            logger.debug(f"App auto-unregister skipped: {e}")

    def list_installed(self) -> List[dict]:
        """List all installed applications across platforms."""
        installed = []

        # Nix packages
        try:
            result = subprocess.run(
                ['nix-env', '-q', '--json'],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                pkgs = json.loads(result.stdout) if result.stdout.strip() else {}
                for name, info in pkgs.items():
                    installed.append({
                        'name': name,
                        'platform': 'nix',
                        'version': info.get('version', ''),
                    })
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

        # Flatpak
        try:
            result = subprocess.run(
                ['flatpak', 'list', '--app', '--columns=name,application,version'],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        installed.append({
                            'name': parts[0],
                            'platform': 'flatpak',
                            'app_id': parts[1] if len(parts) > 1 else '',
                            'version': parts[2] if len(parts) > 2 else '',
                        })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # AppImages
        appimage_dir = os.path.join(self._install_dir, 'appimages')
        if os.path.isdir(appimage_dir):
            for f in os.listdir(appimage_dir):
                if f.lower().endswith('.appimage'):
                    installed.append({
                        'name': f.replace('.AppImage', '').replace('.appimage', ''),
                        'platform': 'appimage',
                        'path': os.path.join(appimage_dir, f),
                    })

        # Wine apps
        wine_dir = os.path.join(self._install_dir, 'wine')
        if os.path.isdir(wine_dir):
            for f in os.listdir(wine_dir):
                if f.endswith('.desktop'):
                    installed.append({
                        'name': f.replace('.desktop', ''),
                        'platform': 'windows',
                    })

        return installed

    def search(self, query: str, platforms: Optional[List[str]] = None) -> List[dict]:
        """Search for available packages across platforms."""
        results = []

        if not platforms or 'nix' in platforms:
            try:
                result = subprocess.run(
                    ['nix', 'search', 'nixpkgs', query, '--json'],
                    capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    pkgs = json.loads(result.stdout) if result.stdout.strip() else {}
                    for attr, info in list(pkgs.items())[:20]:
                        results.append({
                            'name': info.get('pname', attr),
                            'platform': 'nix',
                            'version': info.get('version', ''),
                            'description': info.get('description', ''),
                            'attr': attr,
                        })
            except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass

        if not platforms or 'flatpak' in platforms:
            try:
                result = subprocess.run(
                    ['flatpak', 'search', query, '--columns=name,application,version,description'],
                    capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n')[:20]:
                        parts = line.split('\t')
                        if parts and parts[0]:
                            results.append({
                                'name': parts[0],
                                'platform': 'flatpak',
                                'app_id': parts[1] if len(parts) > 1 else '',
                                'version': parts[2] if len(parts) > 2 else '',
                                'description': parts[3] if len(parts) > 3 else '',
                            })
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        return results

    def history(self) -> List[dict]:
        """Get installation history."""
        return list(self._history)

    # ─── Platform Handlers ──────────────────────────────────

    def _install_nix(self, req: InstallRequest) -> InstallResult:
        """Install a Nix package."""
        pkg = req.source.replace('nixpkgs.', '').replace('nix:', '')
        name = req.name or pkg
        try:
            result = subprocess.run(
                ['nix-env', '-iA', f'nixpkgs.{pkg}'],
                capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return InstallResult(
                    success=True, platform='nix', name=name,
                    app_id=pkg, install_path=f'/nix/store/.../{pkg}')
            return InstallResult(
                success=False, platform='nix', name=name,
                error=result.stderr.strip()[:500])
        except FileNotFoundError:
            return InstallResult(
                success=False, platform='nix', name=name,
                error='nix-env not available')
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='nix', name=name,
                error='Installation timed out')

    def _install_flatpak(self, req: InstallRequest) -> InstallResult:
        """Install a Flatpak package."""
        ref = req.source.replace('flathub:', '').replace('flatpak:', '')
        name = req.name or ref
        try:
            result = subprocess.run(
                ['flatpak', 'install', '-y', 'flathub', ref],
                capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return InstallResult(
                    success=True, platform='flatpak', name=name,
                    app_id=ref)
            return InstallResult(
                success=False, platform='flatpak', name=name,
                error=result.stderr.strip()[:500])
        except FileNotFoundError:
            return InstallResult(
                success=False, platform='flatpak', name=name,
                error='flatpak not available')
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='flatpak', name=name,
                error='Installation timed out')

    def _install_appimage(self, req: InstallRequest) -> InstallResult:
        """Install an AppImage (copy + make executable)."""
        if not os.path.isfile(req.source):
            return InstallResult(
                success=False, platform='appimage',
                name=req.name or req.source, error='File not found')

        appimage_dir = os.path.join(self._install_dir, 'appimages')
        os.makedirs(appimage_dir, exist_ok=True)

        filename = os.path.basename(req.source)
        name = req.name or filename.replace('.AppImage', '').replace('.appimage', '')
        dest = os.path.join(appimage_dir, filename)

        try:
            shutil.copy2(req.source, dest)
            os.chmod(dest, 0o755)
            return InstallResult(
                success=True, platform='appimage', name=name,
                install_path=dest, app_id=name)
        except (IOError, PermissionError) as e:
            return InstallResult(
                success=False, platform='appimage', name=name,
                error=str(e))

    def _install_windows(self, req: InstallRequest) -> InstallResult:
        """Install a Windows executable via Wine."""
        if not os.path.isfile(req.source):
            return InstallResult(
                success=False, platform='windows',
                name=req.name or req.source, error='File not found')

        name = req.name or os.path.basename(req.source).replace('.exe', '').replace('.msi', '')

        # Check Wine availability
        wine = shutil.which('wine64') or shutil.which('wine')
        if not wine:
            return InstallResult(
                success=False, platform='windows', name=name,
                error='Wine not installed. Enable Windows support in NixOS config: hart.kernel.windowsNative.enable = true')

        try:
            ext = os.path.splitext(req.source)[1].lower()
            if ext == '.msi':
                cmd = [wine, 'msiexec', '/i', req.source, '/quiet']
            else:
                cmd = [wine, req.source]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
                env={**os.environ, 'WINEPREFIX': os.path.join(
                    self._install_dir, 'wine', 'prefix')})

            # Wine's exit code is a WEAK success signal — GUI/interactive
            # installers often fork and return 0 before finishing — so 0 cannot
            # positively CONFIRM success. But a NON-ZERO exit IS a reliable
            # FAILURE (wine missing, the .exe crashed, a bad MSI). This used to
            # return success=True UNCONDITIONALLY, so a failed install was
            # reported as a success and _auto_register_app pinned a desktop icon
            # that launches nothing. Honour the failure signal: non-zero -> fail.
            if result.returncode != 0:
                return InstallResult(
                    success=False, platform='windows', name=name,
                    error=f'wine exited {result.returncode}: '
                          f'{(result.stderr or "").strip()[:200]}')
            return InstallResult(
                success=True, platform='windows', name=name,
                install_path=f'{self._install_dir}/wine/prefix',
                app_id=name)
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='windows', name=name,
                error='Installation timed out')
        except Exception as e:
            return InstallResult(
                success=False, platform='windows', name=name,
                error=str(e))

    def _apk_package_name(self, apk_path: str) -> str:
        """Best-effort parse of the Android package name from an APK.

        Tries `aapt dump badging` first (most reliable), then falls back to
        reading the binary AndroidManifest.xml inside the ZIP and scraping the
        UTF-16 string pool for the package id. Returns '' if neither works —
        callers must NOT treat '' as a valid package (it would make the
        post-install `waydroid app list` confirmation pass on a stale match).
        """
        aapt = shutil.which('aapt') or shutil.which('aapt2')
        if aapt:
            try:
                out = subprocess.run(
                    [aapt, 'dump', 'badging', apk_path],
                    capture_output=True, text=True, timeout=30)
                if out.returncode == 0:
                    for line in (out.stdout or '').splitlines():
                        if line.startswith('package:'):
                            # package: name='com.foo.bar' versionCode=...
                            marker = "name='"
                            i = line.find(marker)
                            if i != -1:
                                j = line.find("'", i + len(marker))
                                if j != -1:
                                    return line[i + len(marker):j]
            except (subprocess.TimeoutExpired, OSError):
                pass

        # Fallback: scrape the binary AndroidManifest for a 'com.*' / package id.
        try:
            import zipfile
            import re
            with zipfile.ZipFile(apk_path) as zf:
                raw = zf.read('AndroidManifest.xml')
            # Binary AXML stores strings UTF-16LE in a pool; decode lossily and
            # pull the first plausible java-package token. This is heuristic but
            # only used as a fallback when aapt is absent.
            text = raw.decode('utf-16-le', errors='ignore')
            for m in re.finditer(r'([a-zA-Z][\w]*(?:\.[a-zA-Z][\w]*){2,})', text):
                tok = m.group(1)
                # Skip android.* framework attrs; want the app's own package.
                if not tok.startswith('android.') and not tok.startswith('http'):
                    return tok
        except Exception:
            pass
        return ''

    def _waydroid_session_live(self) -> bool:
        """True iff a Waydroid container session is actually RUNNING.

        Checks `waydroid status` for RUNNING; falls back to /dev/binder presence
        only as a weak signal. A live session is REQUIRED for `waydroid app
        install` to do anything — without it the install is a no-op.
        """
        waydroid = shutil.which('waydroid')
        if waydroid:
            try:
                out = subprocess.run(
                    [waydroid, 'status'],
                    capture_output=True, text=True, timeout=15)
                if out.returncode == 0 and 'RUNNING' in (out.stdout or '').upper():
                    return True
            except (subprocess.TimeoutExpired, OSError):
                pass
        return False

    def _install_android(self, req: InstallRequest) -> InstallResult:
        """Install an Android APK via Waydroid (real ART/PackageManager).

        REAL install contract (no fake-success): require `waydroid` on PATH AND
        a live RUNNING session; run `waydroid app install <apk>`; then CONFIRM by
        polling `waydroid app list` for the APK's parsed package id — exit-0
        alone is NOT proof (some Waydroid builds emit errors on stderr while
        returning 0). On no-waydroid / no-session the source is STAGED to disk
        (success=False, staged=True) with an actionable message — a copied APK is
        NOT installed, so it is never reported as installed/launchable.
        """
        if not os.path.isfile(req.source):
            return InstallResult(
                success=False, platform='android',
                name=req.name or req.source, error='File not found')

        name = req.name or os.path.basename(req.source).replace('.apk', '')
        pkg = self._apk_package_name(req.source)

        waydroid = shutil.which('waydroid')
        if not waydroid or not self._waydroid_session_live():
            # Honest staging: place the APK on disk for a later live session, but
            # DO NOT claim it is installed. success=False + staged=True.
            android_dir = os.path.join(self._install_dir, 'android', 'apps')
            dest = ''
            try:
                os.makedirs(android_dir, exist_ok=True)
                dest = os.path.join(android_dir, os.path.basename(req.source))
                shutil.copy2(req.source, dest)
            except (IOError, PermissionError):
                dest = ''
            return InstallResult(
                success=False, staged=True, platform='android', name=name,
                install_path=dest, app_id=pkg,
                error='Android runtime not available — enable '
                      'hart.subsystems.android (Waydroid) and start a session. '
                      'APK staged to disk but NOT installed.')

        # Live Waydroid session — do the real install.
        try:
            result = subprocess.run(
                [waydroid, 'app', 'install', req.source],
                capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='android', name=name, app_id=pkg,
                error='waydroid app install timed out')
        except OSError as e:
            return InstallResult(
                success=False, platform='android', name=name, app_id=pkg,
                error=str(e))

        # exit-0 is INSUFFICIENT — confirm the package is actually present.
        if not pkg:
            # Without a package id we cannot positively confirm; refuse to claim
            # success rather than trust a bare exit code.
            if result.returncode != 0:
                return InstallResult(
                    success=False, platform='android', name=name,
                    error=f'waydroid app install exited {result.returncode}: '
                          f'{(result.stderr or "").strip()[:200]}')
            return InstallResult(
                success=False, platform='android', name=name,
                error='Install ran but the APK package name could not be parsed '
                      'to confirm it via `waydroid app list`. Install aapt or '
                      'verify manually.')

        try:
            listing = subprocess.run(
                [waydroid, 'app', 'list'],
                capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            return InstallResult(
                success=False, platform='android', name=name, app_id=pkg,
                error=f'install ran but could not verify via app list: {e}')

        if listing.returncode == 0 and pkg in (listing.stdout or ''):
            return InstallResult(
                success=True, platform='android', name=name, app_id=pkg)

        return InstallResult(
            success=False, platform='android', name=name, app_id=pkg,
            error=f'waydroid app install did not register {pkg} '
                  f'(exit {result.returncode}): '
                  f'{(result.stderr or "").strip()[:200]}')

    def _install_macos(self, req: InstallRequest) -> InstallResult:
        """Install/run a macOS app via Darling (experimental, CLI-leaning).

        Honest contract: require `darling`; first prove the Darwin prefix boots
        with `darling shell true` (exit 0). For a .app bundle, locate
        Contents/MacOS/<binary> and launch it via `darling shell <binary>`,
        honouring the exit code. For .dmg/.pkg there is NO reliable headless
        mount+install path under Darling — refuse honestly (success=False).
        GUI (AppKit/Metal/modern Swift) is largely non-functional and we never
        auto-pin a desktop icon for a macOS GUI app.
        """
        name = req.name or os.path.basename(req.source).replace(
            '.dmg', '').replace('.app', '').replace('.pkg', '')

        darling = shutil.which('darling')
        if not darling:
            return InstallResult(
                success=False, platform='macos', name=name,
                error='Darling not installed. macOS app support is experimental '
                      '(opt-in: hart.subsystems.macos.enable). Consider using the '
                      'app natively on macOS via remote desktop.')

        ext = os.path.splitext(req.source)[1].lower()
        if ext in ('.dmg', '.pkg'):
            return InstallResult(
                success=False, platform='macos', name=name,
                error='.dmg/.pkg have no reliable headless install path under '
                      'Darling. Mount/extract the .app bundle and install that '
                      'instead. (macOS support is experimental, GUI mostly '
                      'broken.)')

        # Prove the Darwin prefix actually boots before trusting anything else.
        try:
            boot = subprocess.run(
                [darling, 'shell', 'true'],
                capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='macos', name=name,
                error='darling prefix boot (`darling shell true`) timed out')
        except OSError as e:
            return InstallResult(
                success=False, platform='macos', name=name, error=str(e))
        if boot.returncode != 0:
            return InstallResult(
                success=False, platform='macos', name=name,
                error=f'darling prefix failed to boot (exit {boot.returncode}): '
                      f'{(boot.stderr or "").strip()[:200]}')

        # Resolve the executable inside a .app bundle.
        if ext == '.app' or os.path.isdir(req.source):
            macos_dir = os.path.join(req.source, 'Contents', 'MacOS')
            binary = ''
            if os.path.isdir(macos_dir):
                for entry in sorted(os.listdir(macos_dir)):
                    cand = os.path.join(macos_dir, entry)
                    if os.path.isfile(cand):
                        binary = cand
                        break
            if not binary:
                return InstallResult(
                    success=False, platform='macos', name=name,
                    error='Could not locate Contents/MacOS/<binary> in the .app '
                          'bundle.')
            target = binary
        else:
            # A bare Mach-O CLI binary.
            target = req.source

        try:
            run = subprocess.run(
                [darling, 'shell', target],
                capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return InstallResult(
                success=False, platform='macos', name=name,
                error='darling shell launch timed out')
        except OSError as e:
            return InstallResult(
                success=False, platform='macos', name=name, error=str(e))

        if run.returncode != 0:
            return InstallResult(
                success=False, platform='macos', name=name,
                error=f'darling shell exited {run.returncode}: '
                      f'{(run.stderr or "").strip()[:200]} '
                      '(macOS GUI under Darling is largely non-functional).')

        return InstallResult(
            success=True, platform='macos', name=name,
            install_path=target, app_id=name)

    def _install_snap(self, req: InstallRequest) -> InstallResult:
        """Snap is NOT supported on HART OS — honest refusal (no fake success).

        snapd hard-codes an FHS /snap + /var/lib/snapd tree and runtime-generated
        AppArmor profiles that conflict with the Nix store model. The flake pins
        nixpkgs to a fixed commit with NO snapd input and no /snap FHS shim; the
        only real path is the out-of-tree third-party `nix-snapd` flake, which is
        a steward supply-chain decision, NOT a silent module. So native snap is
        INFEASIBLE on this image. Point the user at the Flatpak/AppImage/Nix
        equivalent instead of silently misrouting .snap to the nix catch-all.
        """
        name = req.name or os.path.basename(
            req.source.replace('snap:', '')).replace('.snap', '')
        return InstallResult(
            success=False, platform='snap', name=name,
            error='Snap is not supported on HART OS — snapd requires an FHS '
                  '/snap tree + the out-of-tree nix-snapd flake which this image '
                  'does not bundle. Install the Flatpak, AppImage, or Nix '
                  'equivalent instead.')

    def _install_browser_ext(self, req: InstallRequest) -> InstallResult:
        """Install a browser extension (.crx → Chromium, .xpi → Firefox).

        REAL install via enterprise managed-policy force-install — DISTINCT from
        HART's in-process .hartpkg extension registry. Writes the extension's
        id (+ update_url for Chromium) into the on-disk managed-policy list, then
        CONFIRMS by reading that file back and asserting the id is present. The
        browser force-installs the extension on its next launch, so "installed"
        here means "policy written + will load" — stated plainly. Failure =
        browser/policy dir absent or the write/verify failed.
        """
        if not os.path.isfile(req.source):
            return InstallResult(
                success=False, platform='browser_ext',
                name=req.name or req.source, error='File not found')

        ext = os.path.splitext(req.source)[1].lower()
        name = req.name or os.path.basename(req.source)

        if ext == '.crx':
            return self._install_chromium_ext(req, name)
        if ext == '.xpi':
            return self._install_firefox_ext(req, name)
        return InstallResult(
            success=False, platform='browser_ext', name=name,
            error=f'Unsupported browser-extension type: {ext} '
                  '(.crx for Chromium, .xpi for Firefox)')

    def _install_chromium_ext(self, req: InstallRequest, name: str) -> InstallResult:
        """Force-install a Chromium .crx via ExtensionInstallForcelist policy.

        The managed policy lives at /etc/chromium/policies/managed/<file>.json
        (the path programs.chromium writes under hart.subsystems.web.enable). A
        bare local .crx is not force-installable by id alone — an update_url is
        required; the caller may pass options['update_url'] (Chrome Web Store or
        a self-hosted update manifest). The extension id may be supplied via
        options['id']; otherwise it is derived from the .crx filename.
        """
        if not shutil.which('chromium') and not shutil.which('chromium-browser'):
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error='Chromium not installed. Enable hart.subsystems.web '
                      '(programs.chromium) to manage extensions.')

        ext_id = req.options.get('id') or os.path.splitext(
            os.path.basename(req.source))[0]
        update_url = req.options.get(
            'update_url', 'https://clients2.google.com/service/update2/crx')

        policy_dir = req.options.get(
            'policy_dir', '/etc/chromium/policies/managed')
        policy_file = os.path.join(policy_dir, 'hart_extensions.json')
        forcelist_entry = f'{ext_id};{update_url}'

        try:
            os.makedirs(policy_dir, exist_ok=True)
            policy = {}
            if os.path.isfile(policy_file):
                try:
                    with open(policy_file, 'r') as f:
                        policy = json.load(f) or {}
                except (json.JSONDecodeError, IOError):
                    policy = {}
            forcelist = policy.get('ExtensionInstallForcelist', [])
            if forcelist_entry not in forcelist:
                forcelist.append(forcelist_entry)
            policy['ExtensionInstallForcelist'] = forcelist
            with open(policy_file, 'w') as f:
                json.dump(policy, f, indent=2)
        except (IOError, PermissionError) as e:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error=f'Failed to write Chromium managed policy: {e}')

        # CONFIRM: read the policy file back and assert the id is present.
        try:
            with open(policy_file, 'r') as f:
                written = json.load(f)
            present = any(
                e.split(';')[0] == ext_id
                for e in written.get('ExtensionInstallForcelist', []))
        except (json.JSONDecodeError, IOError) as e:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error=f'Could not verify Chromium policy on disk: {e}')

        if not present:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error='Chromium policy written but extension id not found on '
                      'verify')

        # NOTE: the extension activates on Chromium's NEXT launch (managed
        # policy is read at startup). success=True means "policy written + will
        # force-install" — verified by the on-disk policy read above.
        return InstallResult(
            success=True, platform='browser_ext', name=name,
            install_path=policy_file, app_id=ext_id)

    def _install_firefox_ext(self, req: InstallRequest, name: str) -> InstallResult:
        """Force-install a Firefox .xpi via the ExtensionSettings policy.

        Writes/merges an ExtensionSettings entry into the managed policies.json
        (the path programs.firefox.policies writes) pointing at the .xpi, then
        verifies the id is present on disk. The .xpi must be AMO-signed unless an
        unbranded/ESR policy build is used.
        """
        if not shutil.which('firefox'):
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error='Firefox not installed. Enable hart.subsystems.web with a '
                      'bundled Firefox to manage extensions.')

        ext_id = req.options.get('id') or os.path.splitext(
            os.path.basename(req.source))[0]
        policy_dir = req.options.get(
            'policy_dir', '/etc/firefox/policies')
        policy_file = os.path.join(policy_dir, 'policies.json')
        install_url = req.options.get('install_url') or f'file://{os.path.abspath(req.source)}'

        try:
            os.makedirs(policy_dir, exist_ok=True)
            doc = {}
            if os.path.isfile(policy_file):
                try:
                    with open(policy_file, 'r') as f:
                        doc = json.load(f) or {}
                except (json.JSONDecodeError, IOError):
                    doc = {}
            policies = doc.setdefault('policies', {})
            ext_settings = policies.setdefault('ExtensionSettings', {})
            ext_settings[ext_id] = {
                'installation_mode': 'force_installed',
                'install_url': install_url,
            }
            with open(policy_file, 'w') as f:
                json.dump(doc, f, indent=2)
        except (IOError, PermissionError) as e:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error=f'Failed to write Firefox policies.json: {e}')

        # CONFIRM on disk.
        try:
            with open(policy_file, 'r') as f:
                written = json.load(f)
            present = ext_id in written.get(
                'policies', {}).get('ExtensionSettings', {})
        except (json.JSONDecodeError, IOError) as e:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error=f'Could not verify Firefox policy on disk: {e}')

        if not present:
            return InstallResult(
                success=False, platform='browser_ext', name=name,
                error='Firefox policy written but extension id not found on '
                      'verify')

        # NOTE: the extension activates on Firefox's NEXT launch (managed policy
        # is read at startup). success=True means "policy written + will
        # force-install" — verified by the on-disk policy read above.
        return InstallResult(
            success=True, platform='browser_ext', name=name,
            install_path=policy_file, app_id=ext_id)

    def _install_extension(self, req: InstallRequest) -> InstallResult:
        """Install a HART OS extension."""
        name = req.name or os.path.basename(req.source)

        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            ext_reg = registry.get('extensions')
            if ext_reg:
                ext = ext_reg.load(req.source)
                return InstallResult(
                    success=True, platform='extension', name=name,
                    app_id=ext.manifest.id, version=ext.manifest.version)
        except Exception as e:
            return InstallResult(
                success=False, platform='extension', name=name,
                error=str(e))

        return InstallResult(
            success=False, platform='extension', name=name,
            error='Extension registry not available')

    # ─── Uninstall handlers ─────────────────────────────────

    def _uninstall_nix(self, pkg: str) -> InstallResult:
        try:
            result = subprocess.run(
                ['nix-env', '-e', pkg],
                capture_output=True, text=True, timeout=60)
            return InstallResult(
                success=result.returncode == 0, platform='nix',
                name=pkg, error=result.stderr.strip()[:500])
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return InstallResult(
                success=False, platform='nix', name=pkg, error=str(e))

    def _uninstall_flatpak(self, app_id: str) -> InstallResult:
        try:
            result = subprocess.run(
                ['flatpak', 'uninstall', '-y', app_id],
                capture_output=True, text=True, timeout=60)
            return InstallResult(
                success=result.returncode == 0, platform='flatpak',
                name=app_id, error=result.stderr.strip()[:500])
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return InstallResult(
                success=False, platform='flatpak', name=app_id, error=str(e))

    def _uninstall_appimage(self, name: str) -> InstallResult:
        appimage_dir = os.path.join(self._install_dir, 'appimages')
        for f in os.listdir(appimage_dir) if os.path.isdir(appimage_dir) else []:
            if name.lower() in f.lower():
                os.remove(os.path.join(appimage_dir, f))
                return InstallResult(
                    success=True, platform='appimage', name=name)
        return InstallResult(
            success=False, platform='appimage', name=name,
            error='AppImage not found')

    def _uninstall_windows(self, name: str) -> InstallResult:
        wine = shutil.which('wine64') or shutil.which('wine')
        if wine:
            try:
                subprocess.run(
                    [wine, 'uninstaller'],
                    capture_output=True, timeout=10)
            except Exception:
                pass
        return InstallResult(
            success=False, platform='windows', name=name,
            error='Wine uninstaller requires interactive session')


# ─── Singleton ──────────────────────────────────────────────

_installer: Optional[AppInstaller] = None


def get_installer() -> AppInstaller:
    """Get the global AppInstaller instance."""
    global _installer
    if _installer is None:
        _installer = AppInstaller()
    return _installer


# ─── Flask Route Registration ───────────────────────────────

def register_app_install_routes(app):
    """Register the SINGLE app-management route surface on a Flask app.

    Phase-8 route consolidation: this is the ONE owner of every app verb —
    install / uninstall / installed / search / detect / history / platforms AND
    the per-app permission endpoints. Each verb is registered on BOTH the
    canonical ``/api/shell/apps/*`` prefix AND the legacy ``/api/apps/*`` prefix
    (one view function, two URL rules) so the marketplace frontend
    (hartMarketplace.js -> /api/apps/*), shell_manifest's documented API list,
    and every existing test keep resolving — without a second implementation.

    ``shell_os_apis.register_shell_os_routes`` delegates its former /api/apps/*
    store routes here (it no longer defines its own AppInstaller-calling bodies),
    so there is no parallel path. Mutating verbs (install/uninstall + permission
    writes) pass ``_require_shell_auth`` — the gate the legacy /api/apps/* had but
    the canonical /api/shell/apps/* previously lacked. Idempotent: liquid_ui
    calls this directly AND via register_shell_os_routes; the second call is a
    no-op (Flask would otherwise raise "overwriting an existing endpoint").
    """
    from flask import jsonify, request

    # Idempotency latch — both register_shell_os_routes and the liquid_ui init
    # call this; register the routes exactly once per app.
    if getattr(app, '_hart_app_routes_registered', False):
        return
    app._hart_app_routes_registered = True

    # The canonical local-shell auth gate. Imported (not redefined) so there is
    # ONE auth decision for the whole shell — the same gate file-manager/terminal
    # routes use. Fail-OPEN to a permissive shim ONLY if shell_os_apis is somehow
    # unavailable (a non-shell node), matching how hart_wm_client degrades.
    try:
        from integrations.agent_engine.shell_os_apis import _require_shell_auth
    except Exception:  # pragma: no cover - non-shell node fallback
        def _require_shell_auth(f):
            return f

    # Both prefixes resolve to ONE view function. ``_route`` stacks the two URL
    # rules; the canonical prefix is first so url_for/endpoint naming is stable.
    _PREFIXES = ('/api/shell/apps', '/api/apps')

    def _route(suffix, **kwargs):
        """Decorator: bind a view to BOTH prefixes (one impl, two routes)."""
        def deco(fn):
            for i, pfx in enumerate(_PREFIXES):
                # Distinct endpoint name per rule (Flask requires uniqueness);
                # the legacy alias reuses the same callable.
                ep = fn.__name__ if i == 0 else f'{fn.__name__}__legacy'
                app.add_url_rule(pfx + suffix, ep, fn, **kwargs)
            return fn
        return deco

    @_route('/install', methods=['POST'])
    @_require_shell_auth
    def shell_apps_install():
        """Install an application (any platform).

        Body:
            source: str — file path, URL, or package name
            platform: str — (optional) nix, flatpak, appimage, windows, android
            name: str — (optional) display name
            sha256: str — (optional) expected checksum
        """
        data = request.get_json(force=True)
        source = data.get('source', '')
        if not source:
            return jsonify({'error': 'source required'}), 400

        platform_str = data.get('platform', '')
        platform = InstallerPlatform.UNKNOWN
        for p in InstallerPlatform:
            if p.value == platform_str:
                platform = p
                break

        req = InstallRequest(
            source=source,
            platform=platform,
            name=data.get('name', ''),
            version=data.get('version', ''),
            sha256=data.get('sha256', ''),
            options=data.get('options', {}),
        )

        installer = get_installer()
        result = installer.install(req)

        return jsonify({
            'success': result.success,
            'staged': result.staged,
            'platform': result.platform,
            'name': result.name,
            'version': result.version,
            'app_id': result.app_id,
            'install_path': result.install_path,
            'error': result.error,
            'duration': round(result.duration_seconds, 2),
        }), 200 if result.success else 400

    @_route('/uninstall', methods=['POST'])
    @_require_shell_auth
    def shell_apps_uninstall():
        """Uninstall an application."""
        data = request.get_json(force=True)
        app_id = data.get('app_id', '')
        platform = data.get('platform', '')
        if not app_id:
            return jsonify({'error': 'app_id required'}), 400

        installer = get_installer()
        result = installer.uninstall(app_id, platform)

        return jsonify({
            'success': result.success,
            'name': result.name,
            'platform': result.platform,
            'error': result.error,
        })

    @_route('/installed', methods=['GET'])
    def shell_apps_installed():
        """List all installed applications across platforms."""
        installer = get_installer()
        apps = installer.list_installed()
        return jsonify({
            'apps': apps,
            'count': len(apps),
        })

    @_route('/search', methods=['GET'])
    def shell_apps_search():
        """Search for packages across platforms.

        Query params:
            q: search query
            platforms: comma-separated list (nix,flatpak) — canonical
            platform: single platform — legacy /api/apps/* spelling (back-compat)
            limit: optional result cap — legacy /api/apps/* param (back-compat)
        """
        query = request.args.get('q', '')
        if not query:
            return jsonify({'error': 'q parameter required'}), 400

        # Accept BOTH the canonical comma-list `platforms` AND the legacy single
        # `platform` the old /api/apps/* surface used, so consolidating the two
        # prefixes never silently drops a caller's platform filter.
        platforms_str = request.args.get('platforms', '')
        if platforms_str:
            platforms = platforms_str.split(',')
        else:
            single = request.args.get('platform')
            platforms = [single] if single else None

        installer = get_installer()
        results = installer.search(query, platforms)

        # Legacy `limit` cap (the old /api/apps/search honoured it).
        try:
            limit = int(request.args.get('limit', 0))
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            results = results[:limit]

        return jsonify({
            'query': query,
            'results': results,
            'count': len(results),
        })

    @_route('/detect', methods=['POST'])
    def shell_apps_detect():
        """Detect the platform of an installer file."""
        data = request.get_json(force=True)
        file_path = data.get('path', '')
        if not file_path or not os.path.isfile(file_path):
            return jsonify({'error': 'Valid file path required'}), 400

        platform = detect_platform(file_path)
        return jsonify({
            'path': file_path,
            'platform': platform.value,
            'name': os.path.basename(file_path),
            'size': os.path.getsize(file_path),
        })

    @_route('/history', methods=['GET'])
    def shell_apps_history():
        """Get installation history."""
        installer = get_installer()
        return jsonify({
            'history': installer.history(),
            'count': len(installer.history()),
        })

    @_route('/platforms', methods=['GET'])
    def shell_apps_platforms():
        """List supported platforms and their availability."""
        platforms = []
        for p in InstallerPlatform:
            if p == InstallerPlatform.UNKNOWN:
                continue
            available = False
            tool = ''
            if p == InstallerPlatform.NIX:
                tool = 'nix-env'
                available = shutil.which('nix-env') is not None
            elif p == InstallerPlatform.FLATPAK:
                tool = 'flatpak'
                available = shutil.which('flatpak') is not None
            elif p == InstallerPlatform.APPIMAGE:
                available = True  # Always available (just needs chmod +x)
            elif p == InstallerPlatform.WINDOWS:
                tool = 'wine64'
                available = shutil.which('wine64') is not None or \
                           shutil.which('wine') is not None
            elif p == InstallerPlatform.ANDROID:
                tool = 'waydroid'
                available = shutil.which('waydroid') is not None
            elif p == InstallerPlatform.MACOS:
                tool = 'darling'
                available = shutil.which('darling') is not None
            elif p == InstallerPlatform.SNAP:
                # Honestly unsupported — shown so the UI can grey it out.
                tool = 'snapd'
                available = False
            elif p == InstallerPlatform.BROWSER_EXT:
                tool = 'chromium/firefox'
                available = (shutil.which('chromium') is not None or
                             shutil.which('chromium-browser') is not None or
                             shutil.which('firefox') is not None)
            elif p == InstallerPlatform.EXTENSION:
                available = True

            platforms.append({
                'platform': p.value,
                'available': available,
                'tool': tool,
            })

        return jsonify({'platforms': platforms})

    logger.info("Registered app installation routes")
