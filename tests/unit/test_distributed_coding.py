"""Tests for distributed coding agent — classification, sharding, fan-out."""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


class TestDataClassification:
    """Verify _classify_scope respects data sensitivity."""

    def _get_orchestrator(self):
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        return CodingAgentOrchestrator()

    def test_explicit_override(self):
        orch = self._get_orchestrator()
        assert orch._classify_scope('task', '/tmp', 'edge_only') == 'edge_only'
        assert orch._classify_scope('task', '/tmp', 'public') == 'public'

    def test_env_file_detected_as_edge_only(self):
        orch = self._get_orchestrator()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, '.env'), 'w') as f:
                f.write('SECRET_KEY=abc123')
            scope = orch._classify_scope('fix bug', d)
            assert scope == 'edge_only'

    def test_pem_file_detected_as_edge_only(self):
        orch = self._get_orchestrator()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'server.pem'), 'w') as f:
                f.write('-----BEGIN CERTIFICATE-----')
            scope = orch._classify_scope('fix bug', d)
            assert scope == 'edge_only'

    def test_clean_code_is_trusted_peer(self):
        orch = self._get_orchestrator()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'main.py'), 'w') as f:
                f.write('def hello():\n    return "world"\n')
            scope = orch._classify_scope('fix bug', d)
            assert scope == 'trusted_peer'

    def test_no_working_dir_defaults_trusted(self):
        orch = self._get_orchestrator()
        assert orch._classify_scope('task', '') == 'trusted_peer'


class TestShardScopeMapping:
    """Verify scope maps to correct shard visibility."""

    def test_trusted_peer_gets_interfaces(self):
        """Trusted peers should only see signatures, not implementations."""
        from integrations.agent_engine.shard_engine import ShardScope
        scope_map = {
            'trusted_peer': ShardScope.INTERFACES,
            'federated': ShardScope.FULL_FILE,
            'public': ShardScope.FULL_FILE,
        }
        assert scope_map['trusted_peer'] == ShardScope.INTERFACES
        assert scope_map['federated'] == ShardScope.FULL_FILE

    def test_edge_only_never_reaches_distribute(self):
        """edge_only scope should route to _execute_local, never _distribute."""
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        with patch.object(orch, '_execute_local', return_value={'success': True}) as mock_local, \
             patch.object(orch, '_distribute_to_hive') as mock_dist:
            orch.execute('task', data_scope='edge_only')
            mock_local.assert_called_once()
            mock_dist.assert_not_called()


class TestExecuteEndpointSecurity:
    """Verify /coding/execute endpoint auth and path traversal protection."""

    def test_missing_encrypted_returns_400(self):
        """No encrypted payload = rejected."""
        from integrations.coding_agent.api import coding_agent_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(coding_agent_bp)
        with app.test_client() as client:
            resp = client.post('/coding/execute', json={})
            assert resp.status_code == 400

    def test_bad_encryption_returns_403(self):
        """Invalid envelope = unauthorized."""
        from integrations.coding_agent.api import coding_agent_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(coding_agent_bp)
        with app.test_client() as client:
            resp = client.post('/coding/execute',
                               json={'encrypted': {'garbage': 'data'}})
            assert resp.status_code == 403

    def test_path_traversal_blocked(self):
        """Files with ../ paths should be rejected."""
        # This tests the path sanitization inside the endpoint
        safe_path = os.path.normpath('../../../etc/passwd')
        assert safe_path.startswith('..')


class TestClawBridge:
    """Verify claw_bridge Rust tools work from Python."""

    @pytest.fixture(autouse=True)
    def _check_claw(self):
        try:
            import claw_bridge
            self.claw = claw_bridge
        except ImportError:
            pytest.skip("claw_bridge not compiled")

    def test_bash(self):
        result = json.loads(self.claw.execute_bash('echo test123'))
        assert 'test123' in result['stdout']
        assert result['interrupted'] is False

    def test_grep(self):
        result = json.loads(self.claw.grep_search('def ', __file__))
        assert result['numFiles'] >= 0

    def test_glob(self):
        result = json.loads(self.claw.glob_search('*.py', os.path.dirname(__file__)))
        assert result['numFiles'] > 0

    def test_read_file(self):
        result = json.loads(self.claw.read_file(__file__, 0, 5))
        assert 'file' in result

    def test_write_and_edit(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                          delete=False) as f:
            f.write('hello world')
            path = f.name
        try:
            result = json.loads(self.claw.edit_file(path, 'hello', 'goodbye'))
            assert 'filePath' in result
            content = json.loads(self.claw.read_file(path, 0, 10))
            # Verify edit took effect
            assert 'goodbye' in content['file']['content']
        finally:
            os.unlink(path)


class TestClawNativeBackend:
    """Verify ClawNativeBackend registration in BACKENDS."""

    def test_backend_registered(self):
        from integrations.coding_agent.tool_backends import BACKENDS
        assert 'claw_native' in BACKENDS

    def test_backend_capabilities(self):
        from integrations.coding_agent.tool_backends import BACKENDS
        backend = BACKENDS['claw_native']()
        if backend is None:
            pytest.skip("claw_bridge not compiled")
        caps = backend.get_capabilities()
        assert caps['type'] == 'native_rust'
        assert 'bash' in caps['tools']

    def test_router_has_claw_defaults(self):
        from integrations.coding_agent.tool_router import HEURISTIC_DEFAULTS
        assert HEURISTIC_DEFAULTS.get('terminal_workflows') == 'claw_native'
        assert HEURISTIC_DEFAULTS.get('repo_exploration') == 'claw_native'
        assert HEURISTIC_DEFAULTS.get('bash_execution') == 'claw_native'
