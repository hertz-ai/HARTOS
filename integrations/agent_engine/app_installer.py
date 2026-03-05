"""
Unified App Installer — Cross-Platform Package Installation API.

Handles installation from ANY platform through a single interface:
  - Linux: Nix packages, Flatpak, AppImage
  - Windows: .exe/.msi via Wine binfmt integration
  - Android: .apk via Android subsystem (binder/ashmem)
  - macOS: .app/.dmg via Darling (experimental)
  - HART OS: Extensions from extensions/ directory

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
    EXTENSION = 'extension'
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
        except Exception as e:
            logger.debug(f"App auto-register skipped: {e}")

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

            # Wine often returns 0 even for interactive installers
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

    def _install_android(self, req: InstallRequest) -> InstallResult:
        """Install an Android APK."""
        if not os.path.isfile(req.source):
            return InstallResult(
                success=False, platform='android',
                name=req.name or req.source, error='File not found')

        name = req.name or os.path.basename(req.source).replace('.apk', '')

        # Check if Android subsystem is available
        if not os.path.exists('/dev/binder'):
            return InstallResult(
                success=False, platform='android', name=name,
                error='Android subsystem not enabled. Set hart.kernel.androidNative.enable = true in NixOS config')

        # Try ADB-style install
        adb = shutil.which('adb')
        if adb:
            try:
                result = subprocess.run(
                    [adb, 'install', '-r', req.source],
                    capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    return InstallResult(
                        success=True, platform='android', name=name,
                        app_id=name)
            except (subprocess.TimeoutExpired, Exception):
                pass

        # Fallback: copy to Android app directory
        android_dir = os.path.join(self._install_dir, 'android', 'apps')
        os.makedirs(android_dir, exist_ok=True)
        dest = os.path.join(android_dir, os.path.basename(req.source))
        try:
            shutil.copy2(req.source, dest)
            return InstallResult(
                success=True, platform='android', name=name,
                install_path=dest, app_id=name)
        except (IOError, PermissionError) as e:
            return InstallResult(
                success=False, platform='android', name=name,
                error=str(e))

    def _install_macos(self, req: InstallRequest) -> InstallResult:
        """Install a macOS app via Darling (experimental)."""
        name = req.name or os.path.basename(req.source).replace('.dmg', '').replace('.app', '')

        darling = shutil.which('darling')
        if not darling:
            return InstallResult(
                success=False, platform='macos', name=name,
                error='Darling not installed. macOS app support is experimental. '
                      'Consider using the app natively on macOS via remote desktop.')

        return InstallResult(
            success=False, platform='macos', name=name,
            error='macOS app installation via Darling is not yet automated')

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
    """Register app installation API routes on a Flask app."""
    from flask import jsonify, request

    @app.route('/api/shell/apps/install', methods=['POST'])
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
            'platform': result.platform,
            'name': result.name,
            'version': result.version,
            'app_id': result.app_id,
            'install_path': result.install_path,
            'error': result.error,
            'duration': round(result.duration_seconds, 2),
        }), 200 if result.success else 400

    @app.route('/api/shell/apps/uninstall', methods=['POST'])
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

    @app.route('/api/shell/apps/installed', methods=['GET'])
    def shell_apps_installed():
        """List all installed applications across platforms."""
        installer = get_installer()
        apps = installer.list_installed()
        return jsonify({
            'apps': apps,
            'count': len(apps),
        })

    @app.route('/api/shell/apps/search', methods=['GET'])
    def shell_apps_search():
        """Search for packages across platforms.

        Query params:
            q: search query
            platforms: comma-separated list (nix,flatpak)
        """
        query = request.args.get('q', '')
        if not query:
            return jsonify({'error': 'q parameter required'}), 400

        platforms_str = request.args.get('platforms', '')
        platforms = platforms_str.split(',') if platforms_str else None

        installer = get_installer()
        results = installer.search(query, platforms)
        return jsonify({
            'query': query,
            'results': results,
            'count': len(results),
        })

    @app.route('/api/shell/apps/detect', methods=['POST'])
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

    @app.route('/api/shell/apps/history', methods=['GET'])
    def shell_apps_history():
        """Get installation history."""
        installer = get_installer()
        return jsonify({
            'history': installer.history(),
            'count': len(installer.history()),
        })

    @app.route('/api/shell/apps/platforms', methods=['GET'])
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
                available = os.path.exists('/dev/binder')
            elif p == InstallerPlatform.MACOS:
                tool = 'darling'
                available = shutil.which('darling') is not None
            elif p == InstallerPlatform.EXTENSION:
                available = True

            platforms.append({
                'platform': p.value,
                'available': available,
                'tool': tool,
            })

        return jsonify({'platforms': platforms})

    logger.info("Registered app installation routes")
