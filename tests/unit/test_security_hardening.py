"""
Tests for Tier 1 Security Hardening:
  1. __pycache__ purge at boot (bytecode injection prevention)
  2. Unsigned gossip peer rejection (enforcement-mode gated)
  3. Announce rate limiting (gossip flood prevention)
  4. Fleet command issuer verification (command spoofing prevention)
  5. RuntimeIntegrityMonitor pycache integration
"""
import json
import os
import sys
import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ═══════════════════════════════════════════════════════════════
# 1. __pycache__ Purge Tests
# ═══════════════════════════════════════════════════════════════

class TestPycachePurge:
    """Verify purge_pycache() deletes __pycache__ dirs and sets env var."""

    def test_purge_creates_and_cleans_pycache_dirs(self, tmp_path):
        """Create fake __pycache__ dirs and verify they are removed."""
        from security.node_integrity import purge_pycache

        # Create nested __pycache__ dirs
        (tmp_path / '__pycache__').mkdir()
        (tmp_path / '__pycache__' / 'foo.cpython-310.pyc').write_text('fake')
        (tmp_path / 'sub' / '__pycache__').mkdir(parents=True)
        (tmp_path / 'sub' / '__pycache__' / 'bar.cpython-310.pyc').write_text('fake')

        count = purge_pycache(str(tmp_path))

        assert count == 2
        assert not (tmp_path / '__pycache__').exists()
        assert not (tmp_path / 'sub' / '__pycache__').exists()
        # Non-pycache dirs should remain
        assert (tmp_path / 'sub').exists()

    def test_purge_sets_pythondontwritebytecode(self, tmp_path):
        from security.node_integrity import purge_pycache
        # Clear env var first
        os.environ.pop('PYTHONDONTWRITEBYTECODE', None)

        purge_pycache(str(tmp_path))

        assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'

    def test_purge_returns_zero_for_clean_tree(self, tmp_path):
        from security.node_integrity import purge_pycache

        # No __pycache__ dirs exist
        (tmp_path / 'clean_module.py').write_text('# clean')
        count = purge_pycache(str(tmp_path))
        assert count == 0

    def test_purge_handles_empty_directory(self, tmp_path):
        from security.node_integrity import purge_pycache

        count = purge_pycache(str(tmp_path))
        assert count == 0


# ═══════════════════════════════════════════════════════════════
# 2. Unsigned Peer Rejection Tests
# ═══════════════════════════════════════════════════════════════

class TestUnsignedPeerRejection:
    """Verify _merge_peer() enforces signature requirements by enforcement mode."""

    def _make_gossip(self):
        """Create a GossipProtocol with predictable node_id."""
        with patch('integrations.social.peer_discovery.GossipProtocol.__init__',
                   lambda self: None):
            from integrations.social.peer_discovery import GossipProtocol
            gp = GossipProtocol.__new__(GossipProtocol)
            gp.node_id = 'test-local-node'
            gp.base_url = 'http://localhost:6777'
            gp.node_name = 'test'
            gp.version = '1.0.0'
            gp.tier = 'flat'
            return gp

    @patch('integrations.social.peer_discovery.requests')
    def test_hard_enforcement_rejects_unsigned_peer(self, mock_req):
        """In hard mode, peers without Ed25519 signature are rejected."""
        gossip = self._make_gossip()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.first.return_value = None

        peer_data = {
            'node_id': 'unsigned-peer-123',
            'url': 'http://10.0.0.5:6777',
            'name': 'bad-bot',
            # No signature, no public_key
        }

        with patch('security.master_key.get_enforcement_mode', return_value='hard'):
            result = gossip._merge_peer(db, peer_data)

        assert result is False

    @patch('integrations.social.peer_discovery.requests')
    def test_soft_enforcement_accepts_unsigned_peer(self, mock_req):
        """In soft mode, unsigned peers are accepted with warning."""
        gossip = self._make_gossip()
        db = MagicMock()
        # No existing peer
        existing_mock = MagicMock()
        existing_mock.integrity_status = 'unverified'
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.first.return_value = None

        peer_data = {
            'node_id': 'unsigned-peer-456',
            'url': 'http://10.0.0.6:6777',
            'name': 'unknown-bot',
        }

        with patch('security.master_key.get_enforcement_mode', return_value='soft'):
            with patch('security.hive_guardrails.get_guardrail_hash',
                       side_effect=ImportError):
                with patch('security.master_key.load_release_manifest',
                           side_effect=ImportError):
                    result = gossip._merge_peer(db, peer_data)

        # Should be accepted (True = new peer added)
        assert result is True

    def test_no_enforcement_module_accepts_all(self):
        """When security.master_key is not importable, accept all peers."""
        gossip = self._make_gossip()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.first.return_value = None

        peer_data = {
            'node_id': 'unsigned-peer-789',
            'url': 'http://10.0.0.7:6777',
            'name': 'dev-node',
        }

        # Patch so get_enforcement_mode raises ImportError
        with patch.dict('sys.modules', {'security.master_key': None}):
            with patch('security.hive_guardrails.get_guardrail_hash',
                       side_effect=ImportError):
                result = gossip._merge_peer(db, peer_data)

        assert result is True


# ═══════════════════════════════════════════════════════════════
# 3. Announce Rate Limiting Tests
# ═══════════════════════════════════════════════════════════════

class TestAnnounceRateLimit:
    """Verify _check_announce_rate() limits per-IP announcement frequency."""

    def setup_method(self):
        """Clear rate limiter state before each test."""
        from integrations.social import discovery
        discovery._ANNOUNCE_RATE.clear()

    def test_allows_under_limit(self):
        from integrations.social.discovery import _check_announce_rate
        for i in range(10):
            assert _check_announce_rate('192.168.1.1') is True

    def test_blocks_over_limit(self):
        from integrations.social.discovery import _check_announce_rate
        for i in range(10):
            _check_announce_rate('192.168.1.2')
        # 11th should be blocked
        assert _check_announce_rate('192.168.1.2') is False

    def test_window_expires_allows_again(self):
        from integrations.social.discovery import (
            _check_announce_rate, _ANNOUNCE_RATE, _RATE_WINDOW,
        )
        # Fill up the limit
        for i in range(10):
            _check_announce_rate('192.168.1.3')
        assert _check_announce_rate('192.168.1.3') is False

        # Manually age all timestamps beyond the window
        _ANNOUNCE_RATE['192.168.1.3'] = [
            time.time() - _RATE_WINDOW - 1 for _ in range(10)
        ]
        assert _check_announce_rate('192.168.1.3') is True

    def test_different_ips_independent(self):
        from integrations.social.discovery import _check_announce_rate
        # Fill up one IP
        for i in range(10):
            _check_announce_rate('10.0.0.1')
        assert _check_announce_rate('10.0.0.1') is False

        # Another IP should be unaffected
        assert _check_announce_rate('10.0.0.2') is True


# ═══════════════════════════════════════════════════════════════
# 4. Fleet Command Issuer Verification Tests
# ═══════════════════════════════════════════════════════════════

class TestFleetIssuerVerification:
    """Verify _verify_issuer() checks PeerNode authority."""

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_self_issued_always_valid(self, _):
        from integrations.social.fleet_command import _verify_issuer
        db = MagicMock()
        assert _verify_issuer(db, 'self-node-id') is True

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_empty_issuer_accepted(self, _):
        """Legacy commands with no issuer are accepted."""
        from integrations.social.fleet_command import _verify_issuer
        db = MagicMock()
        assert _verify_issuer(db, '') is True
        assert _verify_issuer(db, 'unknown') is True

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_unknown_issuer_rejected(self, _):
        from integrations.social.fleet_command import _verify_issuer
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        assert _verify_issuer(db, 'nonexistent-node') is False

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_banned_issuer_rejected(self, _):
        from integrations.social.fleet_command import _verify_issuer
        peer = MagicMock()
        peer.status = 'banned'
        peer.tier = 'central'
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        assert _verify_issuer(db, 'banned-node') is False

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_central_tier_accepted(self, _):
        from integrations.social.fleet_command import _verify_issuer
        peer = MagicMock()
        peer.status = 'active'
        peer.tier = 'central'
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        assert _verify_issuer(db, 'central-node') is True

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_regional_tier_accepted(self, _):
        from integrations.social.fleet_command import _verify_issuer
        peer = MagicMock()
        peer.status = 'active'
        peer.tier = 'regional'
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        assert _verify_issuer(db, 'regional-node') is True

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_local_tier_rejected(self, _):
        """Local/flat tier nodes cannot issue fleet commands."""
        from integrations.social.fleet_command import _verify_issuer
        peer = MagicMock()
        peer.status = 'active'
        peer.tier = 'flat'
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        assert _verify_issuer(db, 'flat-node') is False

    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_dead_issuer_rejected(self, _):
        from integrations.social.fleet_command import _verify_issuer
        peer = MagicMock()
        peer.status = 'dead'
        peer.tier = 'central'
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        assert _verify_issuer(db, 'dead-node') is False


# ═══════════════════════════════════════════════════════════════
# 5. RuntimeIntegrityMonitor Pycache Integration Tests
# ═══════════════════════════════════════════════════════════════

class TestRuntimeMonitorPurge:
    """Verify RuntimeIntegrityMonitor calls purge_pycache at init."""

    @patch('security.node_integrity.purge_pycache')
    @patch('security.node_integrity.compute_file_manifest', return_value={})
    def test_monitor_init_calls_purge(self, mock_manifest, mock_purge):
        from security.runtime_monitor import RuntimeIntegrityMonitor
        manifest = {'code_hash': 'abc123'}
        with tempfile.TemporaryDirectory() as tmp:
            monitor = RuntimeIntegrityMonitor(manifest, code_root=tmp)
        mock_purge.assert_called_once_with(tmp)

    @patch('security.node_integrity.purge_pycache')
    @patch('security.node_integrity.compute_file_manifest')
    def test_monitor_boot_manifest_after_purge(self, mock_manifest, mock_purge):
        """Boot manifest should be computed AFTER pycache purge."""
        call_order = []
        mock_purge.side_effect = lambda *a, **kw: call_order.append('purge')
        mock_manifest.side_effect = lambda *a, **kw: (
            call_order.append('manifest'), {}
        )[1]

        from security.runtime_monitor import RuntimeIntegrityMonitor
        manifest = {'code_hash': 'abc123'}
        with tempfile.TemporaryDirectory() as tmp:
            monitor = RuntimeIntegrityMonitor(manifest, code_root=tmp)

        assert call_order == ['purge', 'manifest']


# ═══════════════════════════════════════════════════════════════
# 6. Integration: get_pending_commands filters by issuer
# ═══════════════════════════════════════════════════════════════

class TestGetPendingCommandsFiltering:
    """Verify get_pending_commands() rejects commands from unverified issuers."""

    @patch('integrations.social.fleet_command._verify_issuer')
    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_verified_command_delivered(self, _, mock_verify):
        from integrations.social.fleet_command import FleetCommandService
        mock_verify.return_value = True

        cmd = MagicMock()
        cmd.id = 1
        cmd.issued_by = 'central-node'
        cmd.status = 'pending'
        cmd.to_dict.return_value = {'id': 1, 'cmd_type': 'config_update'}

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [cmd]

        result = FleetCommandService.get_pending_commands(db, 'target-node')
        assert len(result) == 1
        assert cmd.status == 'delivered'

    @patch('integrations.social.fleet_command._verify_issuer')
    @patch('integrations.social.fleet_command._get_self_node_id',
           return_value='self-node-id')
    def test_unverified_command_rejected(self, _, mock_verify):
        from integrations.social.fleet_command import FleetCommandService
        mock_verify.return_value = False

        cmd = MagicMock()
        cmd.id = 2
        cmd.issued_by = 'fake-node'
        cmd.status = 'pending'

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [cmd]

        result = FleetCommandService.get_pending_commands(db, 'target-node')
        assert len(result) == 0
        assert cmd.status == 'rejected'
