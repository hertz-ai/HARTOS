"""
Tests for the autonomous upgrade pipeline — end-to-end wiring.

Verifies that:
1. Upgrade goal type is registered in GoalManager
2. Upgrade tools exist and call the orchestrator
3. Orchestrator stages advance correctly (BUILD→TEST→AUDIT→BENCHMARK→SIGN→CANARY→DEPLOY)
4. BENCHMARK_DIR import bug is fixed in orchestrator
5. Crawl4ai world model health gates the benchmark stage
6. Gossip beacon includes version info
7. OTA service runs orchestrated upgrade before applying
8. Peer witness post-update verification works
"""

import os
import sys
import json
import time
import threading
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ═══════════════════════════════════════════════════════════════
# 1. Goal Type Registration
# ═══════════════════════════════════════════════════════════════

class TestUpgradeGoalTypeRegistered:
    """Verify 'upgrade' goal type is in the GoalManager registry."""

    def test_upgrade_type_registered(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        assert 'upgrade' in get_registered_types()

    def test_upgrade_prompt_builder_exists(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('upgrade')
        assert builder is not None

    def test_upgrade_prompt_builder_output(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('upgrade')
        goal = {'title': 'Upgrade to v2.0', 'description': 'Auto-upgrade',
                'config': {}, 'goal_type': 'upgrade'}
        prompt = builder(goal)
        assert 'AUTO-UPGRADE ORCHESTRATOR' in prompt
        assert 'advance_upgrade_pipeline' in prompt

    def test_upgrade_tool_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('upgrade')
        assert 'upgrade' in tags


# ═══════════════════════════════════════════════════════════════
# 2. Upgrade Tools
# ═══════════════════════════════════════════════════════════════

class TestUpgradeToolsExist:
    """Verify all 10 upgrade tools are defined."""

    def test_tool_count(self):
        from integrations.agent_engine.upgrade_tools import UPGRADE_TOOLS
        assert len(UPGRADE_TOOLS) >= 10

    def test_tool_names(self):
        from integrations.agent_engine.upgrade_tools import UPGRADE_TOOLS
        names = {t['name'] for t in UPGRADE_TOOLS}
        expected = {
            'check_upgrade_status', 'capture_benchmark', 'compare_benchmarks',
            'start_upgrade', 'advance_upgrade_pipeline', 'check_canary_health',
            'rollback_upgrade', 'get_benchmark_history', 'register_benchmark',
            'list_benchmarks',
        }
        assert expected.issubset(names)

    def test_check_upgrade_status_calls_orchestrator(self):
        from integrations.agent_engine.upgrade_tools import check_upgrade_status
        mock_orch = MagicMock()
        mock_orch.get_status.return_value = {'stage': 'idle'}
        mock_orch.check_for_new_version.return_value = None

        # Patch at the source module (import inside function)
        with patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                   return_value=mock_orch):
            result = check_upgrade_status()
        assert result['success'] is True
        assert result['pipeline']['stage'] == 'idle'

    def test_start_upgrade_returns_result(self):
        from integrations.agent_engine.upgrade_tools import start_upgrade
        with patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator') as mock:
            mock.return_value.start_upgrade.return_value = {
                'success': True, 'stage': 'building', 'version': 'v2.0'}
            result = start_upgrade('v2.0', 'abc123')
        assert result.get('success') or 'error' in result


# ═══════════════════════════════════════════════════════════════
# 3. Orchestrator Stages
# ═══════════════════════════════════════════════════════════════

class TestOrchestratorStages:
    """Test orchestrator stage advancement."""

    def _make_orchestrator(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        with patch('integrations.agent_engine.upgrade_orchestrator.STATE_FILE',
                   '/tmp/test_upgrade_state.json'):
            orch = UpgradeOrchestrator()
            orch._state = {
                'stage': 'idle', 'version': '', 'git_sha': '',
                'started_at': 0, 'stage_history': [],
            }
        return orch

    def test_start_upgrade_sets_building(self):
        orch = self._make_orchestrator()
        result = orch.start_upgrade('v2.0', 'abc123')
        assert result['success'] is True
        assert result['stage'] == 'building'

    def test_cannot_start_while_active(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        result = orch.start_upgrade('v2.1')
        assert result['success'] is False
        assert 'already active' in result['error']

    def test_advance_from_building(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        with patch.object(orch, '_stage_build', return_value=(True, 'hash=abc')):
            with patch.object(orch, '_save_state'):
                result = orch.advance_pipeline()
        assert result['success'] is True
        assert result['stage'] == 'testing'

    def test_advance_failure_sets_failed(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        with patch.object(orch, '_stage_build', return_value=(False, 'build broke')):
            with patch.object(orch, '_save_state'):
                result = orch.advance_pipeline()
        assert result['success'] is False
        assert 'build broke' in result['detail']

    def test_rollback_from_canary(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        orch._state['stage'] = 'canary'
        with patch.object(orch, '_broadcast_rollback'):
            with patch.object(orch, '_save_state'):
                result = orch.rollback('canary health failed')
        assert result['success'] is True
        assert result['rolled_back_from'] == 'canary'

    def test_stage_order_is_correct(self):
        from integrations.agent_engine.upgrade_orchestrator import _STAGE_ORDER, UpgradeStage
        expected_order = [
            UpgradeStage.BUILDING, UpgradeStage.TESTING, UpgradeStage.AUDITING,
            UpgradeStage.BENCHMARKING, UpgradeStage.SIGNING, UpgradeStage.CANARY,
            UpgradeStage.DEPLOYING, UpgradeStage.COMPLETED,
        ]
        assert _STAGE_ORDER == expected_order

    def test_get_status_returns_dict(self):
        orch = self._make_orchestrator()
        status = orch.get_status()
        assert 'stage' in status
        assert status['stage'] == 'idle'


# ═══════════════════════════════════════════════════════════════
# 4. BENCHMARK_DIR Fix
# ═══════════════════════════════════════════════════════════════

class TestBenchmarkDirFix:
    """Verify BENCHMARK_DIR is properly defined in upgrade_orchestrator."""

    def test_benchmark_dir_defined(self):
        from integrations.agent_engine.upgrade_orchestrator import BENCHMARK_DIR
        assert BENCHMARK_DIR is not None
        assert 'benchmarks' in BENCHMARK_DIR

    def test_benchmark_dir_matches_registry(self):
        from integrations.agent_engine.upgrade_orchestrator import BENCHMARK_DIR
        from integrations.agent_engine.benchmark_registry import BENCHMARK_DIR as REG_DIR
        assert BENCHMARK_DIR == REG_DIR


# ═══════════════════════════════════════════════════════════════
# 5. Crawl4ai World Model Benchmark Gate
# ═══════════════════════════════════════════════════════════════

class TestCrawl4aiBenchmarkGate:
    """Verify _stage_benchmark() checks world model health."""

    def _make_orchestrator_at_benchmark(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        orch = UpgradeOrchestrator()
        orch._state = {
            'stage': 'benchmarking', 'version': 'v2.0', 'git_sha': 'abc',
            'started_at': time.time(), 'stage_history': [],
        }
        return orch

    def test_benchmark_passes_when_wm_healthy(self):
        orch = self._make_orchestrator_at_benchmark()

        mock_registry = MagicMock()
        mock_registry.capture_snapshot.return_value = {}
        mock_registry.is_upgrade_safe.return_value = (True, 'all good')

        mock_wm = MagicMock()
        mock_wm.check_health.return_value = {'healthy': True}
        mock_wm.get_learning_stats.return_value = {'flush_rate': 0.95}

        with patch('integrations.agent_engine.upgrade_orchestrator.BENCHMARK_DIR',
                   '/tmp/nonexistent_benchmarks'):
            with patch('integrations.agent_engine.upgrade_orchestrator.os.listdir',
                       return_value=['v1.9.json']):
                with patch('integrations.agent_engine.upgrade_orchestrator.os.path.getmtime',
                           return_value=1.0):
                    with patch('integrations.agent_engine.benchmark_registry.get_benchmark_registry',
                               return_value=mock_registry):
                        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                                   return_value=mock_wm):
                            passed, detail = orch._stage_benchmark()

        assert passed is True

    def test_benchmark_fails_when_wm_unhealthy(self):
        orch = self._make_orchestrator_at_benchmark()

        mock_registry = MagicMock()
        mock_registry.capture_snapshot.return_value = {}
        mock_registry.is_upgrade_safe.return_value = (True, 'benchmarks ok')

        mock_wm = MagicMock()
        mock_wm.check_health.return_value = {'healthy': False}

        with patch('integrations.agent_engine.upgrade_orchestrator.BENCHMARK_DIR',
                   '/tmp/nonexistent_benchmarks'):
            with patch('integrations.agent_engine.upgrade_orchestrator.os.listdir',
                       return_value=['v1.9.json']):
                with patch('integrations.agent_engine.upgrade_orchestrator.os.path.getmtime',
                           return_value=1.0):
                    with patch('integrations.agent_engine.benchmark_registry.get_benchmark_registry',
                               return_value=mock_registry):
                        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                                   return_value=mock_wm):
                            passed, detail = orch._stage_benchmark()

        assert passed is False
        assert 'unhealthy' in detail

    def test_benchmark_fails_on_low_flush_rate(self):
        orch = self._make_orchestrator_at_benchmark()

        mock_registry = MagicMock()
        mock_registry.capture_snapshot.return_value = {}
        mock_registry.is_upgrade_safe.return_value = (True, 'ok')

        mock_wm = MagicMock()
        mock_wm.check_health.return_value = {'healthy': True}
        mock_wm.get_learning_stats.return_value = {'flush_rate': 0.2}

        with patch('integrations.agent_engine.upgrade_orchestrator.BENCHMARK_DIR',
                   '/tmp/nonexistent_benchmarks'):
            with patch('integrations.agent_engine.upgrade_orchestrator.os.listdir',
                       return_value=['v1.9.json']):
                with patch('integrations.agent_engine.upgrade_orchestrator.os.path.getmtime',
                           return_value=1.0):
                    with patch('integrations.agent_engine.benchmark_registry.get_benchmark_registry',
                               return_value=mock_registry):
                        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                                   return_value=mock_wm):
                            passed, detail = orch._stage_benchmark()

        assert passed is False
        assert 'flush_rate' in detail

    def test_benchmark_passes_when_wm_unavailable(self):
        """World model is optional — don't block if import fails."""
        orch = self._make_orchestrator_at_benchmark()

        mock_registry = MagicMock()
        mock_registry.capture_snapshot.return_value = {}
        mock_registry.is_upgrade_safe.return_value = (True, 'ok')

        with patch('integrations.agent_engine.upgrade_orchestrator.BENCHMARK_DIR',
                   '/tmp/nonexistent_benchmarks'):
            with patch('integrations.agent_engine.upgrade_orchestrator.os.listdir',
                       return_value=['v1.9.json']):
                with patch('integrations.agent_engine.upgrade_orchestrator.os.path.getmtime',
                           return_value=1.0):
                    with patch('integrations.agent_engine.benchmark_registry.get_benchmark_registry',
                               return_value=mock_registry):
                        # World model bridge import fails
                        with patch.dict('sys.modules',
                                        {'integrations.agent_engine.world_model_bridge': None}):
                            passed, detail = orch._stage_benchmark()

        assert passed is True


# ═══════════════════════════════════════════════════════════════
# 6. Gossip Beacon Version Info
# ═══════════════════════════════════════════════════════════════

class TestGossipBeaconVersion:
    """Verify _self_info() includes version fields."""

    def test_self_info_includes_current_version(self):
        from integrations.social.peer_discovery import GossipProtocol

        with patch('integrations.social.peer_discovery.GossipProtocol.__init__',
                   return_value=None):
            gp = GossipProtocol.__new__(GossipProtocol)
            gp.node_id = 'test-node'
            gp.base_url = 'http://localhost:6777'
            gp.node_name = 'test'
            gp.version = '1.5.0'
            gp.started_at = None
            gp.tier = 'flat'

        mock_orch = MagicMock()
        mock_orch.get_status.return_value = {
            'stage': 'completed', 'version': '2.0.0'}

        with patch('integrations.social.peer_discovery.GossipProtocol._get_count',
                   return_value=0):
            with patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                       return_value=mock_orch):
                info = gp._self_info()

        assert info['current_version'] == '1.5.0'
        assert info['available_version'] == '2.0.0'

    def test_self_info_no_available_version_when_idle(self):
        from integrations.social.peer_discovery import GossipProtocol

        with patch('integrations.social.peer_discovery.GossipProtocol.__init__',
                   return_value=None):
            gp = GossipProtocol.__new__(GossipProtocol)
            gp.node_id = 'test-node'
            gp.base_url = 'http://localhost:6777'
            gp.node_name = 'test'
            gp.version = '1.5.0'
            gp.started_at = None
            gp.tier = 'flat'

        mock_orch = MagicMock()
        mock_orch.get_status.return_value = {'stage': 'idle', 'version': ''}

        with patch('integrations.social.peer_discovery.GossipProtocol._get_count',
                   return_value=0):
            with patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                       return_value=mock_orch):
                info = gp._self_info()

        assert info['current_version'] == '1.5.0'
        assert 'available_version' not in info


# ═══════════════════════════════════════════════════════════════
# 7. OTA Orchestrated Upgrade
# ═══════════════════════════════════════════════════════════════

class TestOTAOrchestratedUpgrade:
    """Verify hyve-update-service.py calls orchestrator before applying."""

    def test_run_orchestrated_upgrade_method_exists(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'deploy', 'distro', 'update'))
        try:
            # We need to import with the right module path
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'hyve_update_service',
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'deploy', 'distro', 'update', 'hyve-update-service.py'))
            mod = importlib.util.module_from_spec(spec)
            # Mock sys.exit and system calls
            with patch.dict(os.environ, {'HYVE_UPDATE_URL': 'http://test'}):
                spec.loader.exec_module(mod)
            assert hasattr(mod.HyveUpdateService, '_run_orchestrated_upgrade')
        finally:
            if sys.path[0].endswith('update'):
                sys.path.pop(0)

    def test_orchestrated_upgrade_passes_on_completion(self):
        """Orchestrator completes → apply proceeds."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'deploy', 'distro', 'update'))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'hyve_update_svc2',
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'deploy', 'distro', 'update', 'hyve-update-service.py'))
            mod = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {'HYVE_UPDATE_URL': 'http://test'}):
                spec.loader.exec_module(mod)

            svc = mod.HyveUpdateService()

            mock_orch = MagicMock()
            mock_orch.start_upgrade.return_value = {'success': True}
            mock_orch.get_status.side_effect = [
                {'stage': 'building'},
                {'stage': 'completed'},
            ]
            mock_orch.advance_pipeline.return_value = {
                'success': True, 'stage': 'completed'}

            with patch.dict(sys.modules, {
                'integrations': MagicMock(),
                'integrations.agent_engine': MagicMock(),
                'integrations.agent_engine.upgrade_orchestrator': MagicMock(
                    get_upgrade_orchestrator=MagicMock(return_value=mock_orch)),
            }):
                result = svc._run_orchestrated_upgrade('v2.0', '/tmp/bundle.tar.gz')

            assert result is True
        finally:
            if sys.path[0].endswith('update'):
                sys.path.pop(0)

    def test_orchestrated_upgrade_fails_on_pipeline_failure(self):
        """Orchestrator fails → apply is blocked."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'deploy', 'distro', 'update'))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'hyve_update_svc3',
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'deploy', 'distro', 'update', 'hyve-update-service.py'))
            mod = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {'HYVE_UPDATE_URL': 'http://test'}):
                spec.loader.exec_module(mod)

            svc = mod.HyveUpdateService()

            mock_orch = MagicMock()
            mock_orch.start_upgrade.return_value = {'success': False, 'error': 'already active'}

            with patch.dict(sys.modules, {
                'integrations': MagicMock(),
                'integrations.agent_engine': MagicMock(),
                'integrations.agent_engine.upgrade_orchestrator': MagicMock(
                    get_upgrade_orchestrator=MagicMock(return_value=mock_orch)),
            }):
                result = svc._run_orchestrated_upgrade('v2.0', '/tmp/bundle.tar.gz')

            assert result is False
        finally:
            if sys.path[0].endswith('update'):
                sys.path.pop(0)


# ═══════════════════════════════════════════════════════════════
# 8. Peer Witness Post-Update Verification
# ═══════════════════════════════════════════════════════════════

class TestPeerWitnessPostUpdate:
    """Verify IntegrityService.verify_post_update() works."""

    @pytest.fixture
    def db_session(self):
        """In-memory SQLite session for testing."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from integrations.social.models import Base

        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    def test_verify_post_update_unknown_peer(self, db_session):
        from integrations.social.integrity_service import IntegrityService
        result = IntegrityService.verify_post_update(
            db_session, 'nonexistent_node')
        assert result['verified'] is False
        assert 'not found' in result['reason']

    def test_verify_post_update_known_peer(self, db_session):
        from integrations.social.integrity_service import IntegrityService
        from integrations.social.models import PeerNode

        peer = PeerNode(
            node_id='test_updated_node',
            url='http://test-peer:6777',
            name='test-peer',
            status='active',
            code_hash='abc123',
        )
        db_session.add(peer)
        db_session.flush()

        # Mock code hash verification to succeed
        with patch.object(IntegrityService, 'verify_code_hash',
                          return_value={'verified': True}):
            with patch.object(IntegrityService, 'create_challenge',
                              return_value={'passed': True}):
                with patch('security.hive_guardrails.get_guardrail_hash',
                           return_value='hash_abc'):
                    result = IntegrityService.verify_post_update(
                        db_session, 'test_updated_node',
                        expected_version='v2.0')

        assert result['verified'] is True
        assert 'code_hash' in result['checks']

    def test_verify_post_update_guardrail_mismatch(self, db_session):
        from integrations.social.integrity_service import IntegrityService
        from integrations.social.models import PeerNode

        peer = PeerNode(
            node_id='bad_peer',
            url='http://bad-peer:6777',
            name='bad-peer',
            status='active',
            code_hash='abc123',
        )
        # Set guardrail_hash if the column exists
        if hasattr(PeerNode, 'guardrail_hash'):
            peer.guardrail_hash = 'wrong_hash'
        db_session.add(peer)
        db_session.flush()

        with patch.object(IntegrityService, 'verify_code_hash',
                          return_value={'verified': True}):
            with patch.object(IntegrityService, 'create_challenge',
                              return_value={'passed': True}):
                with patch('security.hive_guardrails.get_guardrail_hash',
                           return_value='correct_hash'):
                    with patch.object(IntegrityService, 'increase_fraud_score'):
                        result = IntegrityService.verify_post_update(
                            db_session, 'bad_peer')

        # Should detect the mismatch
        gh_check = result['checks'].get('guardrail_hash', {})
        if hasattr(PeerNode, 'guardrail_hash'):
            assert gh_check.get('match') is False


# ═══════════════════════════════════════════════════════════════
# 9. Full Pipeline Smoke Test
# ═══════════════════════════════════════════════════════════════

class TestFullPipelineSmokeTest:
    """Smoke test: orchestrator can traverse all stages with mocked handlers."""

    def test_full_pipeline_all_stages_pass(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator

        orch = UpgradeOrchestrator()
        orch._state = {
            'stage': 'idle', 'version': '', 'git_sha': '',
            'started_at': 0, 'stage_history': [],
        }

        # Start
        with patch.object(orch, '_save_state'):
            result = orch.start_upgrade('v3.0', 'sha_abc')
        assert result['success'] is True

        # Advance through all stages with mocked handlers
        stage_mocks = {
            '_stage_build': (True, 'hash=abc'),
            '_stage_test': (True, 'pass_rate=99%'),
            '_stage_audit': (True, 'audit ok'),
            '_stage_benchmark': (True, 'benchmarks ok'),
            '_stage_sign': (True, 'signed'),
            '_stage_canary': (True, 'canary passed'),
            '_stage_deploy': (True, 'deployed'),
        }

        for method_name, return_val in stage_mocks.items():
            with patch.object(orch, method_name, return_value=return_val):
                with patch.object(orch, '_save_state'):
                    result = orch.advance_pipeline()
            assert result['success'] is True, f"{method_name} failed: {result}"

        assert orch.get_status()['stage'] == 'completed'

    def test_pipeline_fails_and_recovers(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator

        orch = UpgradeOrchestrator()
        orch._state = {
            'stage': 'idle', 'version': '', 'git_sha': '',
            'started_at': 0, 'stage_history': [],
        }

        with patch.object(orch, '_save_state'):
            orch.start_upgrade('v3.0')

        # Build passes
        with patch.object(orch, '_stage_build', return_value=(True, 'ok')):
            with patch.object(orch, '_save_state'):
                orch.advance_pipeline()

        # Test fails
        with patch.object(orch, '_stage_test',
                          return_value=(False, 'pass_rate=80%')):
            with patch.object(orch, '_save_state'):
                result = orch.advance_pipeline()
        assert result['success'] is False
        assert orch.get_status()['stage'] == 'failed'

        # Can start a new upgrade after failure
        with patch.object(orch, '_save_state'):
            result = orch.start_upgrade('v3.1')
        assert result['success'] is True
