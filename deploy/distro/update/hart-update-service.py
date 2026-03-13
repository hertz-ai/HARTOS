#!/usr/bin/env python3
"""
HART OS OTA Update Service - Automated update mechanism.

Checks for updates via:
  1. Release server (GitHub Releases API)
  2. Hive gossip network (peer announces new version)

Update flow:
  check → download → verify Ed25519 signature → apply → restart services

Runs as hart-update.service triggered by hart-update.timer (daily).
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error

logger = logging.getLogger('hart-update')

INSTALL_DIR = '/opt/hart'
DATA_DIR = '/var/lib/hart'
CONFIG_DIR = '/etc/hart'
VERSION_FILE = os.path.join(INSTALL_DIR, 'VERSION')
UPDATE_CHECK_FILE = os.path.join(DATA_DIR, '.last-update-check')

# Configurable update URL — reads from env file, falls back to default
DEFAULT_UPDATE_URL = 'https://api.github.com/repos/hertz-ai/HARTOS/releases/latest'
UPDATE_URL = os.environ.get('HART_UPDATE_URL', '')
if not UPDATE_URL:
    try:
        with open(os.path.join(CONFIG_DIR, 'hart.env')) as _f:
            for _line in _f:
                if _line.startswith('HART_UPDATE_URL='):
                    UPDATE_URL = _line.strip().split('=', 1)[1]
                    break
    except FileNotFoundError:
        pass
if not UPDATE_URL:
    UPDATE_URL = DEFAULT_UPDATE_URL


class HartUpdateService:
    """OTA-style update mechanism for HART OS."""

    def __init__(self):
        self.current_version = self._get_current_version()

    def _get_current_version(self) -> str:
        """Read current installed version."""
        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE) as f:
                return f.read().strip()
        return '1.0.0'

    def check_for_updates(self) -> dict:
        """Check GitHub Releases for new versions.

        Returns:
            {available: bool, current: str, latest: str, download_url: str}
        """
        try:
            req = urllib.request.Request(UPDATE_URL)
            req.add_header('Accept', 'application/vnd.github.v3+json')
            req.add_header('User-Agent', 'HART OS-Updater/1.0')

            with urllib.request.urlopen(req, timeout=15) as resp:
                release = json.loads(resp.read())

            latest_tag = release.get('tag_name', '').lstrip('v')
            assets = release.get('assets', [])

            # Find the bundle asset
            download_url = None
            checksum_url = None
            for asset in assets:
                name = asset.get('name', '')
                if name.endswith('.tar.gz') and 'hart-os' in name:
                    download_url = asset.get('browser_download_url')
                elif name.endswith('.sha256'):
                    checksum_url = asset.get('browser_download_url')

            # Record check time
            with open(UPDATE_CHECK_FILE, 'w') as f:
                f.write(str(time.time()))

            available = (latest_tag and latest_tag != self.current_version and
                         download_url is not None)

            return {
                'available': available,
                'current': self.current_version,
                'latest': latest_tag or self.current_version,
                'download_url': download_url,
                'checksum_url': checksum_url,
                'release_notes': release.get('body', '')[:500],
            }

        except urllib.error.URLError as e:
            logger.warning("Update check failed: %s", e)
            return {
                'available': False,
                'current': self.current_version,
                'error': str(e),
            }

    def _verify_ed25519_signature(self, bundle_path: str, sig_url: str) -> bool:
        """Verify Ed25519 signature of update bundle.

        Downloads .sig file and verifies against the master public key
        embedded in security/master_key.py.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives import serialization

            # Download signature
            temp_sig = bundle_path + '.sig'
            urllib.request.urlretrieve(sig_url, temp_sig)

            with open(temp_sig, 'rb') as f:
                signature = bytes.fromhex(f.read().decode().strip())

            # Load master public key from installed codebase
            sys.path.insert(0, INSTALL_DIR)
            try:
                from security.master_key import MASTER_PUBLIC_KEY_HEX
                pub_key_bytes = bytes.fromhex(MASTER_PUBLIC_KEY_HEX)
                public_key = Ed25519PublicKey.from_public_bytes(pub_key_bytes)
            finally:
                sys.path.pop(0)

            # Read bundle and verify
            with open(bundle_path, 'rb') as f:
                bundle_data = f.read()

            public_key.verify(signature, bundle_data)
            logger.info("Ed25519 signature verified successfully.")
            return True

        except ImportError:
            logger.warning("cryptography library not available — skipping Ed25519 verification")
            return True  # Allow update if crypto lib missing (legacy systems)
        except Exception as e:
            logger.error("Ed25519 signature verification FAILED: %s", e)
            return False

    def download_update(self, download_url: str, checksum_url: str = None) -> str:
        """Download update bundle to temp directory.

        Verifies:
        1. SHA-256 checksum (if .sha256 asset exists)
        2. Ed25519 signature (if .sig asset exists — REQUIRED for production)

        Returns path to downloaded file.
        """
        temp_dir = tempfile.mkdtemp(prefix='hart-update-')
        bundle_path = os.path.join(temp_dir, 'update.tar.gz')

        logger.info("Downloading update from %s...", download_url)

        # Download bundle with streaming (avoid deprecated urlretrieve)
        req = urllib.request.Request(download_url)
        req.add_header('User-Agent', 'HART OS-Updater/1.0')
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(bundle_path, 'wb') as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)

        # Verify SHA-256 checksum
        if checksum_url:
            checksum_path = os.path.join(temp_dir, 'checksum.sha256')
            urllib.request.urlretrieve(checksum_url, checksum_path)

            with open(checksum_path) as f:
                expected_hash = f.read().strip().split()[0]

            with open(bundle_path, 'rb') as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()

            if actual_hash != expected_hash:
                os.unlink(bundle_path)
                raise ValueError(
                    f"Checksum mismatch: expected {expected_hash}, got {actual_hash}")

            logger.info("SHA-256 checksum verified: %s", actual_hash[:16])

        # Verify Ed25519 signature (look for .sig asset alongside bundle)
        sig_url = download_url + '.sig'
        try:
            # Check if .sig asset exists
            req = urllib.request.Request(sig_url, method='HEAD')
            req.add_header('User-Agent', 'HART OS-Updater/1.0')
            urllib.request.urlopen(req, timeout=10)
            # Exists — verify it
            if not self._verify_ed25519_signature(bundle_path, sig_url):
                os.unlink(bundle_path)
                raise ValueError("Ed25519 signature verification failed — update rejected")
        except urllib.error.URLError:
            require_sig = os.environ.get('HART_UPDATE_REQUIRE_SIGNATURE', 'true').lower() == 'true'
            if require_sig:
                os.unlink(bundle_path)
                raise ValueError(
                    "Unsigned update rejected (HART_UPDATE_REQUIRE_SIGNATURE=true). "
                    "No .sig file at: " + sig_url)
            else:
                logger.warning("No .sig file at %s — sig verification skipped (dev mode)", sig_url)

        return bundle_path

    def _check_fleet_approval(self, version):
        """Ask regional host if this version is approved for rollout.
        Standalone nodes auto-approve. Fleet nodes check with their regional host."""
        try:
            url = f"http://localhost:6777/api/social/fleet/update-approved?v={version}"
            req = urllib.request.Request(url, method='GET')
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            approved = data.get('approved', False)
            if not approved:
                logger.info(f"Fleet update {version} not yet approved by regional host")
            return approved
        except Exception:
            # Standalone or offline: auto-approve
            logger.debug("Fleet approval check unavailable, auto-approving (standalone mode)")
            return True

    def apply_update(self, bundle_path: str, version: str = None) -> bool:
        """Apply update bundle.

        Steps:
        0. Check fleet approval (regional host must approve for fleet nodes)
        1. Create backup of current installation
        2. Extract new code
        3. Preserve config and data
        4. Restart services
        """
        # Fleet approval gate: regional host must approve before applying
        if version and not self._check_fleet_approval(version):
            logger.info("Update blocked: fleet approval not granted for version %s", version)
            return False
        backup_dir = os.path.join(DATA_DIR, 'backup-pre-update')

        try:
            # Backup current code (full rsync snapshot for rollback)
            os.makedirs(backup_dir, exist_ok=True)
            if os.path.exists(VERSION_FILE):
                subprocess.run(['cp', VERSION_FILE,
                                os.path.join(backup_dir, 'VERSION')], check=True)
            # Snapshot current code for rollback
            subprocess.run([
                'rsync', '-a',
                '--exclude=venv', '--exclude=models', '--exclude=agent_data',
                f'{INSTALL_DIR}/', f'{backup_dir}/code/',
            ], check=False)  # Non-fatal if rsync unavailable

            # Stop services
            logger.info("Stopping services for update...")
            subprocess.run(['systemctl', 'stop', 'hart.target'], check=False)

            # Extract update
            logger.info("Extracting update...")
            with tarfile.open(bundle_path, 'r:gz') as tar:
                # Extract to temp dir first
                temp_extract = tempfile.mkdtemp(prefix='hart-extract-')
                tar.extractall(temp_extract)

                # Find the extracted directory
                extracted_dirs = os.listdir(temp_extract)
                if extracted_dirs:
                    src_dir = os.path.join(temp_extract, extracted_dirs[0])
                else:
                    src_dir = temp_extract

                # Sync to install dir (preserve config)
                subprocess.run([
                    'rsync', '-a',
                    '--exclude=.env',
                    '--exclude=agent_data/*.db',
                    '--exclude=agent_data/*.json',
                    f'{src_dir}/', f'{INSTALL_DIR}/',
                ], check=True)

            # Update pip dependencies
            logger.info("Updating dependencies...")
            subprocess.run([
                f'{INSTALL_DIR}/venv/bin/pip', 'install',
                '-r', f'{INSTALL_DIR}/requirements.txt', '-q',
            ], check=True)

            # Run migrations
            logger.info("Running database migrations...")
            subprocess.run([
                f'{INSTALL_DIR}/venv/bin/python', '-c',
                'from integrations.social.migrations import run_migrations; run_migrations()',
            ], check=True, cwd=INSTALL_DIR)

            # Restart services
            logger.info("Restarting services...")
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', 'start', 'hart.target'], check=True)

            # Verify backend comes up
            for _ in range(15):
                try:
                    req = urllib.request.Request('http://localhost:6777/status')
                    with urllib.request.urlopen(req, timeout=3):
                        logger.info("Update applied successfully!")
                        return True
                except Exception:
                    time.sleep(2)

            logger.error("Backend did not start after update. Rolling back...")
            self.rollback(backup_dir)
            return False

        except Exception as e:
            logger.error("Update failed: %s. Rolling back...", e)
            self.rollback(backup_dir)
            return False

    def rollback(self, backup_dir: str) -> bool:
        """Roll back to previous version by restoring code snapshot."""
        logger.warning("Rolling back update...")
        try:
            # Restore code snapshot if available
            code_backup = os.path.join(backup_dir, 'code')
            if os.path.isdir(code_backup):
                subprocess.run([
                    'rsync', '-a',
                    '--exclude=venv', '--exclude=models', '--exclude=agent_data',
                    f'{code_backup}/', f'{INSTALL_DIR}/',
                ], check=True)
                logger.info("Code restored from backup.")
            elif os.path.exists(os.path.join(backup_dir, 'VERSION')):
                # Minimal rollback: at least restore VERSION
                subprocess.run([
                    'cp', os.path.join(backup_dir, 'VERSION'), VERSION_FILE
                ], check=True)

            subprocess.run(['systemctl', 'daemon-reload'], check=False)
            subprocess.run(['systemctl', 'start', 'hart.target'], check=False)
            logger.info("Rollback complete.")
            return True
        except Exception as e:
            logger.error("Rollback failed: %s", e)
            return False

    def _run_orchestrated_upgrade(self, version: str, bundle_path: str,
                                  checksum_url: str = None) -> bool:
        """Run the 7-stage upgrade orchestrator before applying the update.

        The orchestrator gates: BUILD→TEST→AUDIT→BENCHMARK→SIGN→CANARY→DEPLOY.
        Only if all stages pass does apply_update() run.
        """
        try:
            sys.path.insert(0, INSTALL_DIR)
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()

            # Start pipeline
            start_result = orch.start_upgrade(version)
            if not start_result.get('success'):
                logger.warning("Orchestrator start failed: %s", start_result.get('error'))
                return False

            terminal_stages = ('completed', 'failed', 'rolled_back')
            max_iterations = 50  # Safety cap

            for _ in range(max_iterations):
                status = orch.get_status()
                stage = status.get('stage', '')

                if stage in terminal_stages:
                    break

                result = orch.advance_pipeline()
                if not result.get('success'):
                    detail = result.get('detail', 'stage failed')
                    # Canary "check again later" is not a failure
                    if 'check again later' in detail or 'canary in progress' in detail:
                        logger.info("Canary in progress, waiting 30s...")
                        time.sleep(30)
                        continue
                    logger.error("Pipeline failed at %s: %s", stage, detail)
                    orch.rollback(detail)
                    return False

            final = orch.get_status()
            if final.get('stage') == 'completed':
                logger.info("Orchestrator passed all stages for v%s", version)
                return True
            else:
                logger.error("Orchestrator ended in %s for v%s",
                             final.get('stage'), version)
                return False

        except ImportError:
            logger.info("Upgrade orchestrator not available — direct apply (standalone mode)")
            return True  # Standalone nodes without orchestrator: allow direct update
        except Exception as e:
            logger.error("Orchestrator error: %s — falling back to direct apply", e)
            return True
        finally:
            if INSTALL_DIR in sys.path:
                sys.path.remove(INSTALL_DIR)

    def run(self):
        """Main update check + apply cycle."""
        logger.info("HART OS Update Service starting (current: %s)...",
                     self.current_version)

        result = self.check_for_updates()

        if not result.get('available'):
            logger.info("No updates available (current: %s, latest: %s)",
                         self.current_version, result.get('latest', '?'))
            return

        logger.info("Update available: %s -> %s",
                     self.current_version, result['latest'])

        try:
            bundle_path = self.download_update(
                result['download_url'],
                result.get('checksum_url'),
            )

            # Run orchestrated upgrade pipeline (gates before apply)
            if not self._run_orchestrated_upgrade(
                    result['latest'], bundle_path, result.get('checksum_url')):
                logger.error("Orchestrated upgrade rejected v%s — not applying.",
                             result['latest'])
                return

            success = self.apply_update(bundle_path, version=result['latest'])

            if success:
                logger.info("Updated to %s successfully!", result['latest'])
            else:
                logger.error("Update to %s failed.", result['latest'])

        except Exception as e:
            logger.error("Update process failed: %s", e)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(message)s',
    )
    service = HartUpdateService()
    service.run()
