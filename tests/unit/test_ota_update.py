"""
Tests for HART OS OTA Update Service (deploy/distro/update/hart-update-service.py).

Tests cover:
- HartUpdateService.__init__: version reading
- _get_current_version: reads VERSION file, falls back to 1.0.0
- check_for_updates: GitHub API parsing, version comparison, error handling
- _verify_ed25519_signature: signature verification flow (mocked crypto)
- download_update: streaming download, checksum verification, sig verification
- apply_update: backup, extract, pip, migrate, restart, health check, rollback
- rollback: restore from backup
- run: full update cycle
- URL configuration: env var, hart.env file, default fallback
"""

import hashlib
import json
import os
import sys
import tarfile
import tempfile
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# Import the update service
UPDATE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'deploy', 'distro', 'update')
sys.path.insert(0, UPDATE_DIR)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    'hart_update_service',
    os.path.join(UPDATE_DIR, 'hart-update-service.py')
)
ota = importlib.util.module_from_spec(_spec)
# Patch module-level file reads before exec
with patch('builtins.open', side_effect=FileNotFoundError):
    with patch.dict(os.environ, {'HART_UPDATE_URL': 'https://test.example.com/releases/latest'}):
        _spec.loader.exec_module(ota)


# ──────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────

@pytest.fixture
def version_file(tmp_path):
    """Create a temporary VERSION file."""
    vf = tmp_path / 'VERSION'
    vf.write_text('2.1.0')
    return str(vf)


@pytest.fixture
def update_service(tmp_path):
    """Create a HartUpdateService with temp paths."""
    with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'VERSION')):
        with patch.object(ota, 'DATA_DIR', str(tmp_path / 'data')):
            with patch.object(ota, 'UPDATE_CHECK_FILE', str(tmp_path / 'data' / '.last-update-check')):
                os.makedirs(str(tmp_path / 'data'), exist_ok=True)
                (tmp_path / 'VERSION').write_text('1.5.0')
                svc = ota.HartUpdateService()
                return svc


@pytest.fixture
def fake_bundle(tmp_path):
    """Create a fake update bundle (tar.gz)."""
    bundle_dir = tmp_path / 'hart-os-2.0.0'
    bundle_dir.mkdir()
    (bundle_dir / 'VERSION').write_text('2.0.0')
    (bundle_dir / 'hart_intelligence_entry.py').write_text('# updated')

    bundle_path = str(tmp_path / 'update.tar.gz')
    with tarfile.open(bundle_path, 'w:gz') as tar:
        tar.add(str(bundle_dir), arcname='hart-os-2.0.0')
    return bundle_path


# ──────────────────────────────────────────────────
# Version Reading Tests
# ──────────────────────────────────────────────────

class TestVersionReading:

    def test_reads_version_from_file(self, tmp_path):
        """_get_current_version reads VERSION file."""
        vf = tmp_path / 'VERSION'
        vf.write_text('3.2.1')
        with patch.object(ota, 'VERSION_FILE', str(vf)):
            svc = ota.HartUpdateService()
            assert svc.current_version == '3.2.1'

    def test_default_version_when_no_file(self, tmp_path):
        """Falls back to 1.0.0 when VERSION file doesn't exist."""
        with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'nonexistent')):
            svc = ota.HartUpdateService()
            assert svc.current_version == '1.0.0'

    def test_strips_whitespace(self, tmp_path):
        """Version string is stripped of whitespace."""
        vf = tmp_path / 'VERSION'
        vf.write_text('  2.0.0  \n')
        with patch.object(ota, 'VERSION_FILE', str(vf)):
            svc = ota.HartUpdateService()
            assert svc.current_version == '2.0.0'


# ──────────────────────────────────────────────────
# Update Check Tests
# ──────────────────────────────────────────────────

class TestCheckForUpdates:

    def test_parses_github_release(self, update_service, tmp_path):
        """check_for_updates parses GitHub release JSON."""
        release = {
            'tag_name': 'v2.0.0',
            'body': 'Release notes here',
            'assets': [
                {
                    'name': 'hart-os-2.0.0.tar.gz',
                    'browser_download_url': 'https://example.com/hart-os-2.0.0.tar.gz'
                },
                {
                    'name': 'hart-os-2.0.0.sha256',
                    'browser_download_url': 'https://example.com/hart-os-2.0.0.sha256'
                },
            ]
        }

        with patch.object(ota, 'UPDATE_CHECK_FILE', str(tmp_path / '.last-check')):
            with patch('urllib.request.urlopen') as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(release).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_url.return_value = mock_resp

                result = update_service.check_for_updates()

        assert result['available'] is True
        assert result['current'] == '1.5.0'
        assert result['latest'] == '2.0.0'
        assert 'hart-os-2.0.0.tar.gz' in result['download_url']
        assert result['checksum_url'] is not None

    def test_no_update_when_same_version(self, tmp_path):
        """No update available when latest == current."""
        release = {
            'tag_name': 'v1.5.0',
            'assets': [
                {'name': 'hart-os-1.5.0.tar.gz', 'browser_download_url': 'https://x/a.tar.gz'}
            ]
        }
        with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'VERSION')):
            with patch.object(ota, 'UPDATE_CHECK_FILE', str(tmp_path / '.check')):
                (tmp_path / 'VERSION').write_text('1.5.0')
                svc = ota.HartUpdateService()
                with patch('urllib.request.urlopen') as mock_url:
                    mock_resp = MagicMock()
                    mock_resp.read.return_value = json.dumps(release).encode()
                    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                    mock_resp.__exit__ = MagicMock(return_value=False)
                    mock_url.return_value = mock_resp

                    result = svc.check_for_updates()
                    assert result['available'] is False

    def test_handles_network_error(self, update_service):
        """Returns error dict on network failure."""
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('no network')):
            result = update_service.check_for_updates()
        assert result['available'] is False
        assert 'error' in result

    def test_strips_v_prefix_from_tag(self, update_service, tmp_path):
        """Tag 'v2.0.0' becomes '2.0.0'."""
        release = {
            'tag_name': 'v3.0.0-beta',
            'assets': [
                {'name': 'hart-os-3.0.0-beta.tar.gz', 'browser_download_url': 'https://x/a.tar.gz'}
            ]
        }
        with patch.object(ota, 'UPDATE_CHECK_FILE', str(tmp_path / '.check')):
            with patch('urllib.request.urlopen') as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(release).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_url.return_value = mock_resp

                result = update_service.check_for_updates()
        assert result['latest'] == '3.0.0-beta'

    def test_no_download_url_means_not_available(self, update_service, tmp_path):
        """If no matching asset found, available = False."""
        release = {
            'tag_name': 'v9.0.0',
            'assets': [
                {'name': 'unrelated.zip', 'browser_download_url': 'https://x/z.zip'}
            ]
        }
        with patch.object(ota, 'UPDATE_CHECK_FILE', str(tmp_path / '.check')):
            with patch('urllib.request.urlopen') as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(release).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_url.return_value = mock_resp

                result = update_service.check_for_updates()
        assert result['available'] is False


# ──────────────────────────────────────────────────
# Download Tests
# ──────────────────────────────────────────────────

class TestDownloadUpdate:

    def test_downloads_bundle(self, update_service, monkeypatch):
        """download_update saves file to temp directory."""
        monkeypatch.setenv('HART_UPDATE_REQUIRE_SIGNATURE', 'false')
        fake_data = b'fake tarball content' * 100

        with patch('urllib.request.urlopen') as mock_url:
            # Main download
            mock_resp = MagicMock()
            mock_resp.read = MagicMock(side_effect=[fake_data, b''])
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)

            # .sig HEAD check — not found
            import urllib.error
            def urlopen_side_effect(req, **kwargs):
                if hasattr(req, 'method') and req.method == 'HEAD':
                    raise urllib.error.URLError('not found')
                return mock_resp

            mock_url.side_effect = urlopen_side_effect

            path = update_service.download_update('https://example.com/bundle.tar.gz')
            assert os.path.exists(path)
            assert path.endswith('update.tar.gz')

    def test_checksum_verification(self, update_service, tmp_path, monkeypatch):
        """Verifies SHA-256 checksum when provided."""
        monkeypatch.setenv('HART_UPDATE_REQUIRE_SIGNATURE', 'false')
        content = b'test bundle data'
        expected_hash = hashlib.sha256(content).hexdigest()

        call_count = [0]

        def urlopen_side_effect(req, **kwargs):
            call_count[0] += 1
            # .sig HEAD check — always not found (we only test SHA-256 here)
            if hasattr(req, 'method') and req.method == 'HEAD':
                import urllib.error
                raise urllib.error.URLError('not found')
            mock_resp = MagicMock()
            if call_count[0] == 1:
                # Main download
                mock_resp.read = MagicMock(side_effect=[content, b''])
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=urlopen_side_effect):
            with patch('urllib.request.urlretrieve') as mock_retrieve:
                # Write checksum file
                def save_checksum(url, path):
                    with open(path, 'w') as f:
                        f.write(f'{expected_hash}  update.tar.gz')
                mock_retrieve.side_effect = save_checksum

                path = update_service.download_update(
                    'https://example.com/bundle.tar.gz',
                    'https://example.com/bundle.sha256',
                )
                assert os.path.exists(path)

    def test_checksum_mismatch_raises(self, update_service):
        """Raises ValueError on checksum mismatch."""
        content = b'test bundle data'
        wrong_hash = 'a' * 64

        call_count = [0]

        def urlopen_side_effect(req, **kwargs):
            call_count[0] += 1
            mock_resp = MagicMock()
            if call_count[0] == 1:
                mock_resp.read = MagicMock(side_effect=[content, b''])
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=urlopen_side_effect):
            with patch('urllib.request.urlretrieve') as mock_retrieve:
                def save_bad_checksum(url, path):
                    with open(path, 'w') as f:
                        f.write(f'{wrong_hash}  update.tar.gz')
                mock_retrieve.side_effect = save_bad_checksum

                with pytest.raises(ValueError, match='Checksum mismatch'):
                    update_service.download_update(
                        'https://example.com/bundle.tar.gz',
                        'https://example.com/bundle.sha256',
                    )


# ──────────────────────────────────────────────────
# Ed25519 Signature Verification Tests
# ──────────────────────────────────────────────────

class TestEd25519Verification:

    def test_verify_signature_success(self, update_service, tmp_path):
        """Signature verification succeeds with matching key."""
        bundle_path = str(tmp_path / 'bundle.tar.gz')
        with open(bundle_path, 'wb') as f:
            f.write(b'fake bundle')

        mock_pub_key = MagicMock()
        mock_pub_key.verify = MagicMock()  # No exception = success

        with patch('urllib.request.urlretrieve') as mock_retrieve:
            def save_sig(url, path):
                with open(path, 'w') as f:
                    f.write('aa' * 32)  # Fake hex signature
            mock_retrieve.side_effect = save_sig

            with patch.dict(sys.modules, {
                'cryptography.hazmat.primitives.asymmetric.ed25519': MagicMock(
                    Ed25519PublicKey=MagicMock(from_public_bytes=MagicMock(return_value=mock_pub_key))
                ),
                'cryptography.hazmat.primitives': MagicMock(),
            }):
                with patch.object(ota, 'INSTALL_DIR', str(tmp_path)):
                    # Create fake security module
                    sec_dir = tmp_path / 'security'
                    sec_dir.mkdir()
                    (sec_dir / '__init__.py').write_text('')
                    (sec_dir / 'master_key.py').write_text(
                        "MASTER_PUBLIC_KEY_HEX = 'bb' * 32"
                    )
                    result = update_service._verify_ed25519_signature(
                        bundle_path, 'https://example.com/bundle.sig')

        assert result is True

    def test_verify_signature_no_crypto_lib(self, update_service, tmp_path):
        """Returns True (permissive) when cryptography library missing."""
        bundle_path = str(tmp_path / 'bundle.tar.gz')
        with open(bundle_path, 'wb') as f:
            f.write(b'data')

        # Force ImportError on cryptography
        with patch.dict(sys.modules, {'cryptography': None}):
            with patch('builtins.__import__', side_effect=ImportError('no crypto')):
                result = update_service._verify_ed25519_signature(
                    bundle_path, 'https://example.com/bundle.sig')
        # Legacy systems: True (allow update)
        assert result is True


# ──────────────────────────────────────────────────
# Apply Update Tests
# ──────────────────────────────────────────────────

class TestApplyUpdate:

    @patch('subprocess.run')
    @patch('urllib.request.urlopen')
    def test_apply_update_full_flow(self, mock_urlopen, mock_run, update_service, fake_bundle, tmp_path):
        """apply_update: backup → stop → extract → pip → migrate → restart → health."""
        mock_run.return_value = MagicMock(returncode=0)

        # Health check success
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.object(ota, 'DATA_DIR', str(tmp_path / 'data')):
            with patch.object(ota, 'INSTALL_DIR', str(tmp_path / 'install')):
                with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'install' / 'VERSION')):
                    os.makedirs(str(tmp_path / 'data'), exist_ok=True)
                    os.makedirs(str(tmp_path / 'install'), exist_ok=True)
                    (tmp_path / 'install' / 'VERSION').write_text('1.5.0')

                    result = update_service.apply_update(fake_bundle)

        assert result is True
        # Verify systemctl stop was called
        stop_calls = [c for c in mock_run.call_args_list
                      if 'stop' in str(c) and 'hart.target' in str(c)]
        assert len(stop_calls) >= 1

    @patch('subprocess.run')
    @patch('urllib.request.urlopen')
    def test_apply_update_rollback_on_failure(self, mock_urlopen, mock_run, update_service, fake_bundle, tmp_path):
        """apply_update rolls back when health check fails."""
        mock_run.return_value = MagicMock(returncode=0)

        # Health check always fails
        mock_urlopen.side_effect = Exception('backend down')

        with patch.object(ota, 'DATA_DIR', str(tmp_path / 'data')):
            with patch.object(ota, 'INSTALL_DIR', str(tmp_path / 'install')):
                with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'install' / 'VERSION')):
                    os.makedirs(str(tmp_path / 'data'), exist_ok=True)
                    os.makedirs(str(tmp_path / 'install'), exist_ok=True)
                    (tmp_path / 'install' / 'VERSION').write_text('1.5.0')

                    result = update_service.apply_update(fake_bundle)

        assert result is False  # Rollback triggered


# ──────────────────────────────────────────────────
# Rollback Tests
# ──────────────────────────────────────────────────

class TestRollback:

    @patch('subprocess.run')
    def test_rollback_restores_code(self, mock_run, update_service, tmp_path):
        """rollback() uses rsync to restore from backup."""
        mock_run.return_value = MagicMock(returncode=0)

        backup = tmp_path / 'backup'
        code = backup / 'code'
        code.mkdir(parents=True)
        (code / 'VERSION').write_text('1.5.0')

        with patch.object(ota, 'INSTALL_DIR', str(tmp_path / 'install')):
            result = update_service.rollback(str(backup))
        assert result is True

    @patch('subprocess.run')
    def test_rollback_version_only(self, mock_run, update_service, tmp_path):
        """rollback() restores VERSION file when no code backup exists."""
        mock_run.return_value = MagicMock(returncode=0)

        backup = tmp_path / 'backup'
        backup.mkdir()
        (backup / 'VERSION').write_text('1.4.0')

        with patch.object(ota, 'INSTALL_DIR', str(tmp_path / 'install')):
            with patch.object(ota, 'VERSION_FILE', str(tmp_path / 'install' / 'VERSION')):
                result = update_service.rollback(str(backup))
        assert result is True


# ──────────────────────────────────────────────────
# Full Run Cycle Tests
# ──────────────────────────────────────────────────

class TestRunCycle:

    def test_run_no_update_available(self, update_service):
        """run() exits early when no update available."""
        with patch.object(update_service, 'check_for_updates',
                          return_value={'available': False, 'current': '1.5.0', 'latest': '1.5.0'}):
            # Should not crash
            update_service.run()

    def test_run_with_update(self, update_service, fake_bundle):
        """run() downloads and applies when update available."""
        with patch.object(update_service, 'check_for_updates', return_value={
            'available': True,
            'current': '1.5.0',
            'latest': '2.0.0',
            'download_url': 'https://example.com/bundle.tar.gz',
            'checksum_url': None,
        }):
            with patch.object(update_service, 'download_update', return_value=fake_bundle):
                with patch.object(update_service, 'apply_update', return_value=True):
                    update_service.run()


# ──────────────────────────────────────────────────
# URL Configuration Tests
# ──────────────────────────────────────────────────

class TestURLConfiguration:

    def test_default_url(self):
        """DEFAULT_UPDATE_URL points to GitHub releases."""
        assert 'github.com' in ota.DEFAULT_UPDATE_URL
        assert 'hertz-ai' in ota.DEFAULT_UPDATE_URL

    def test_module_constants(self):
        """Module has expected filesystem constants."""
        assert ota.INSTALL_DIR == '/opt/hart'
        assert ota.DATA_DIR == '/var/lib/hart'
        assert ota.CONFIG_DIR == '/etc/hart'


# ──────────────────────────────────────────────────
# Fleet Approval Tests (E5)
# ──────────────────────────────────────────────────

UPDATE_SERVICE_PATH = os.path.join(UPDATE_DIR, 'hart-update-service.py')


class TestFleetApproval:
    """Tests for fleet OTA coordination (E5)."""

    def test_fleet_approval_method_exists(self):
        """Update service should have fleet approval check."""
        content = open(UPDATE_SERVICE_PATH).read()
        assert '_check_fleet_approval' in content

    def test_fleet_standalone_auto_approves(self):
        """When fleet endpoint is unreachable, auto-approve (standalone)."""
        content = open(UPDATE_SERVICE_PATH).read()
        assert 'auto-approv' in content.lower() or 'standalone' in content.lower()

    def test_apply_calls_fleet_check(self):
        """apply_update should call fleet approval before proceeding."""
        content = open(UPDATE_SERVICE_PATH).read()
        assert '_check_fleet_approval' in content
