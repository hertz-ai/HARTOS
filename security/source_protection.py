"""
Source Protection Service — HevolveAI integrity verification.

Multi-layer defense for HevolveAI source code:
  1. pip install: SSH key required (git+ssh://)
  2. Nunba bundling: .pyc only (source stripped)
  3. Boot verification: hash manifest signed by build node
  4. Runtime gating: certificate tier + CCT gates feature access

This module answers:
  - Is HevolveAI installed? How? (SSH, HTTPS, wheel, bundled)
  - Is the source code visible? (Should be False in production)
  - Does the installed code match the known-good manifest?

If integrity check fails → disable in-process mode, force HTTP fallback.
"""
import hashlib
import importlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve_security')

# Path to the known-good manifest (populated by CI/CD build)
_MANIFEST_PATH = os.environ.get(
    'HEVOLVE_HEVOLVEAI_MANIFEST',
    os.path.join(os.path.dirname(__file__), 'hevolveai_manifest.json'),
)


class SourceProtectionService:
    """Verifies HevolveAI installation integrity.

    Called at boot and periodically to ensure the installed HevolveAI
    code matches the signed manifest.  Mismatch → HTTP fallback only.
    """

    @staticmethod
    def check_install_method() -> str:
        """Detect how HevolveAI was installed.

        Returns one of:
            'git_ssh'       — pip install from SSH URL
            'git_https'     — pip install from HTTPS URL
            'pip_wheel'     — installed from a wheel/sdist
            'bundled_pyc'   — .pyc only (Nunba build)
            'bundled_cython'— .so/.pyd (Cython compiled)
            'not_installed' — HevolveAI not found
            'unknown'       — detected but method unclear
        """
        try:
            spec = importlib.util.find_spec('hevolveai')
        except (ModuleNotFoundError, ValueError):
            return 'not_installed'

        if spec is None:
            return 'not_installed'

        origin = spec.origin or ''

        # Check for compiled extensions
        if origin.endswith(('.so', '.pyd')):
            return 'bundled_cython'

        # Check for bytecode only
        if origin.endswith('.pyc'):
            return 'bundled_pyc'

        # Check pip metadata for install source
        try:
            from importlib.metadata import metadata as pkg_metadata
            meta = pkg_metadata('hevolveai')
            # direct_url.json is set by pip for VCS installs
            try:
                from importlib.metadata import packages_distributions
                dist_info = Path(spec.origin).parent
                direct_url = dist_info.parent / (
                    dist_info.name.replace('.', '-') + '.dist-info'
                ) / 'direct_url.json'
                if direct_url.exists():
                    url_data = json.loads(direct_url.read_text())
                    url = url_data.get('url', '')
                    if url.startswith('ssh://') or 'git@' in url:
                        return 'git_ssh'
                    if url.startswith('https://'):
                        return 'git_https'
            except Exception:
                pass

            # Fallback: check installer
            installer = meta.get('Installer', '')
            if installer:
                return 'pip_wheel'
        except Exception:
            pass

        if origin.endswith('.py'):
            return 'unknown'

        return 'unknown'

    @staticmethod
    def is_source_visible() -> bool:
        """Check if HevolveAI .py source files are present.

        In production (Nunba builds), only .pyc should exist.
        Returns True if .py source is found (bad for production).
        """
        try:
            spec = importlib.util.find_spec('hevolveai')
        except (ModuleNotFoundError, ValueError):
            return False

        if spec is None or not spec.origin:
            return False

        # If the spec origin itself is .py, source is visible
        if spec.origin.endswith('.py'):
            return True

        # Check subpackages for .py files
        if spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                loc_path = Path(loc)
                if loc_path.exists():
                    py_files = list(loc_path.glob('**/*.py'))
                    # Exclude __init__.py stubs (often left as .py)
                    real_py = [f for f in py_files
                               if f.name != '__init__.py']
                    if real_py:
                        return True
        return False

    @staticmethod
    def verify_hevolveai_integrity() -> Dict:
        """Verify installed HevolveAI against known-good manifest.

        Returns:
            {
                'verified': bool,
                'install_method': str,
                'source_visible': bool,
                'mismatched_files': list,
                'missing_files': list,
                'extra_files': list,
            }
        """
        result: Dict = {
            'verified': False,
            'install_method': SourceProtectionService.check_install_method(),
            'source_visible': SourceProtectionService.is_source_visible(),
            'mismatched_files': [],
            'missing_files': [],
            'extra_files': [],
        }

        if result['install_method'] == 'not_installed':
            result['error'] = 'HevolveAI not installed'
            return result

        # Load manifest
        manifest = SourceProtectionService._load_manifest()
        if manifest is None:
            result['error'] = 'manifest not found or invalid'
            # No manifest = cannot verify = fail-closed
            result['verified'] = False
            return result

        # Find HevolveAI package root
        try:
            spec = importlib.util.find_spec('hevolveai')
            if spec is None or not spec.submodule_search_locations:
                result['error'] = 'cannot locate HevolveAI package'
                return result
            pkg_root = Path(list(spec.submodule_search_locations)[0])
        except Exception as e:
            result['error'] = f'package location error: {e}'
            return result

        # Compare file hashes
        expected = manifest.get('files', {})
        actual = SourceProtectionService._compute_package_hashes(pkg_root)

        for rel_path, expected_hash in expected.items():
            actual_hash = actual.pop(rel_path, None)
            if actual_hash is None:
                result['missing_files'].append(rel_path)
            elif actual_hash != expected_hash:
                result['mismatched_files'].append(rel_path)

        result['extra_files'] = list(actual.keys())

        # Verified if no mismatches or missing files
        result['verified'] = (
            len(result['mismatched_files']) == 0
            and len(result['missing_files']) == 0
        )

        return result

    @staticmethod
    def _load_manifest() -> Optional[Dict]:
        """Load the signed manifest file."""
        try:
            with open(_MANIFEST_PATH, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _compute_package_hashes(pkg_root: Path) -> Dict[str, str]:
        """Compute SHA-256 hashes for all files in the package."""
        hashes: Dict[str, str] = {}
        if not pkg_root.exists():
            return hashes

        for path in sorted(pkg_root.rglob('*')):
            if path.is_file() and not path.name.startswith('.'):
                rel = str(path.relative_to(pkg_root)).replace('\\', '/')
                h = hashlib.sha256()
                try:
                    with open(path, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b''):
                            h.update(chunk)
                    hashes[rel] = h.hexdigest()
                except (IOError, OSError):
                    pass
        return hashes


def compute_dependency_hash(package_name: str) -> Optional[str]:
    """Compute a combined SHA-256 hash of all files in an installed package.

    Useful for node_integrity to include dependency hashes in the
    overall code hash for tamper detection.

    Args:
        package_name: pip package name (e.g. 'hevolveai' / HevolveAI)

    Returns:
        hex digest string or None if package not found
    """
    try:
        spec = importlib.util.find_spec(package_name)
    except (ModuleNotFoundError, ValueError):
        return None

    if spec is None or not spec.submodule_search_locations:
        return None

    pkg_root = Path(list(spec.submodule_search_locations)[0])
    if not pkg_root.exists():
        return None

    combined = hashlib.sha256()
    for path in sorted(pkg_root.rglob('*')):
        if path.is_file() and not path.name.startswith('.'):
            try:
                with open(path, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b''):
                        combined.update(chunk)
            except (IOError, OSError):
                pass

    digest = combined.hexdigest()
    return digest if digest != hashlib.sha256().hexdigest() else None


class CrawlIntegrityWatcher:
    """Periodic re-verification of HevolveAI package integrity post-boot.

    Mirrors RuntimeIntegrityMonitor's pattern but scoped to the HevolveAI
    package only.  On tamper detection, fires registered callbacks instead
    of halting the hive — callers decide how to respond (e.g. disable
    in-process mode, fall back to HTTP).

    Env vars:
        HEVOLVE_TAMPER_CHECK_INTERVAL  — seconds between checks (default 300)
        HEVOLVE_TAMPER_CHECK_SKIP      — set 'true' to disable (read-only FS)
    """

    def __init__(self, check_interval: int = None):
        self._check_interval = check_interval or int(
            os.environ.get('HEVOLVE_TAMPER_CHECK_INTERVAL', '300'))
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._tampered = False
        self._callbacks: List[Callable] = []
        # Snapshot the hash at construction (boot) time
        self._boot_hash: str = self._compute_current_hash()

    # ── Public API ──────────────────────────────────────────────

    def register_tamper_callback(self, callback: Callable) -> None:
        """Register a callable invoked when tampering is detected.

        Called exactly once per watcher lifetime (stops after first detection).
        """
        with self._lock:
            self._callbacks.append(callback)

    def start(self) -> None:
        """Start the background monitoring thread (daemon=True)."""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._check_loop, daemon=True,
            name='crawl_integrity_watcher')
        self._thread.start()
        logger.info(
            f"[CrawlIntegrityWatcher] Started "
            f"(interval={self._check_interval}s, "
            f"boot_hash={self._boot_hash[:16]}...)"
            if self._boot_hash else
            "[CrawlIntegrityWatcher] Started (HevolveAI not installed)")

    def stop(self) -> None:
        """Stop the watcher gracefully."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    @property
    def is_healthy(self) -> bool:
        """False if tampering was detected."""
        return not self._tampered

    # ── Internal loop ────────────────────────────────────────────

    def _check_loop(self) -> None:
        """Background loop: re-hash HevolveAI every interval."""
        if os.environ.get('HEVOLVE_TAMPER_CHECK_SKIP', '').lower() == 'true':
            logger.info(
                "[CrawlIntegrityWatcher] Checks disabled "
                "(HEVOLVE_TAMPER_CHECK_SKIP)")
            return

        while self._running:
            time.sleep(self._check_interval)
            if not self._running:
                break
            try:
                current = self._compute_current_hash()
                if current and self._boot_hash and current != self._boot_hash:
                    logger.critical(
                        f"[CrawlIntegrityWatcher] TAMPERING DETECTED: "
                        f"HevolveAI hash changed from "
                        f"{self._boot_hash[:16]}... "
                        f"to {current[:16]}...")
                    self._tampered = True
                    self._on_tamper_detected()
                    return  # Stop after first detection
            except Exception as e:
                logger.warning(
                    f"[CrawlIntegrityWatcher] Integrity check error: {e}")

    def _on_tamper_detected(self) -> None:
        """Fire all registered callbacks."""
        with self._lock:
            callbacks = list(self._callbacks)
            self._running = False
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(
                    f"[CrawlIntegrityWatcher] Callback error: {e}")

    def _compute_current_hash(self) -> str:
        """Compute combined SHA-256 over all HevolveAI package files."""
        return compute_dependency_hash('hevolveai') or ''

    # ── Test helper ──────────────────────────────────────────────

    def _check_once_for_test(self) -> None:
        """Run a single hash comparison without sleeping (testing only)."""
        try:
            current = self._compute_current_hash()
            if current and self._boot_hash and current != self._boot_hash:
                self._tampered = True
                self._on_tamper_detected()
        except Exception:
            pass
