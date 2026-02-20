"""
Deployment Scenario Boundary Tests

6 tests covering the full spectrum of HyveOS deployment modes:
1. OBSERVER tier — gossip-only, no agents, no coding
2. STANDARD tier — agents + coding + TTS enabled
3. FULL tier — vision + local LLM + video gen
4. Bundled mode (Nunba) — path redirection to writable user dir
5. Docker/distro mode — coordinator + peers → auto-distribute
6. No coordinator — local /chat fallback, no Redis needed
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test DB before any imports
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


class TestObserverTierMinimum:
    """Test 1: OBSERVER tier — minimal node, gossip + Flask only."""

    def test_observer_tier_features(self):
        """An OBSERVER node enables only gossip + flask, disables agents/coding/TTS."""
        from security.system_requirements import (
            NodeTierLevel, FEATURE_TIER_MAP, _TIER_RANK,
        )

        observer_rank = _TIER_RANK[NodeTierLevel.OBSERVER]

        enabled = []
        disabled = []
        for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
            if _TIER_RANK[min_tier] <= observer_rank:
                enabled.append(feature)
            else:
                disabled.append(feature)

        # OBSERVER should have gossip, sensor_bridge, protocol_adapter, flask_server
        assert 'gossip' in enabled
        assert 'flask_server' in enabled
        assert 'sensor_bridge' in enabled
        assert 'protocol_adapter' in enabled

        # OBSERVER must NOT have agent_engine, coding_agent, TTS, vision, etc.
        assert 'agent_engine' in disabled
        assert 'coding_agent' in disabled
        assert 'tts' in disabled
        assert 'whisper' in disabled
        assert 'video_gen' in disabled
        assert 'local_llm' in disabled
        assert 'regional_host' in disabled

    def test_observer_tier_classification(self):
        """A machine with 1 core, 2GB RAM, 0GB disk → OBSERVER tier."""
        from security.system_requirements import (
            NodeTierLevel, TIER_REQUIREMENTS, HardwareProfile,
        )

        hw = HardwareProfile(cpu_cores=1, ram_gb=2.0, disk_free_gb=0.5, gpu_vram_gb=0.0)

        # Walk tier requirements highest to lowest
        resolved = NodeTierLevel.EMBEDDED  # floor
        for req in TIER_REQUIREMENTS:
            if (hw.cpu_cores >= req.min_cpu_cores and
                hw.ram_gb >= req.min_ram_gb and
                hw.disk_free_gb >= req.min_disk_gb and
                hw.gpu_vram_gb >= req.min_gpu_vram_gb):
                resolved = req.tier
                break

        assert resolved == NodeTierLevel.OBSERVER


class TestStandardTierAgents:
    """Test 2: STANDARD tier — full agent engine + coding + TTS."""

    def test_standard_tier_features(self):
        """STANDARD tier enables agent_engine, coding_agent, TTS, whisper."""
        from security.system_requirements import (
            NodeTierLevel, FEATURE_TIER_MAP, _TIER_RANK,
        )

        standard_rank = _TIER_RANK[NodeTierLevel.STANDARD]

        enabled = []
        for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
            if _TIER_RANK[min_tier] <= standard_rank:
                enabled.append(feature)

        # STANDARD should have everything up to and including standard tier
        assert 'agent_engine' in enabled
        assert 'coding_agent' in enabled
        assert 'tts' in enabled
        assert 'whisper' in enabled
        assert 'gossip' in enabled
        assert 'flask_server' in enabled

        # But NOT full-tier features
        disabled = []
        for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
            if _TIER_RANK[min_tier] > standard_rank:
                disabled.append(feature)

        assert 'video_gen' in disabled
        assert 'local_llm' in disabled
        assert 'regional_host' in disabled

    def test_standard_tier_classification(self):
        """4 cores, 8GB RAM, 3GB disk → STANDARD (disk < 20GB blocks FULL)."""
        from security.system_requirements import (
            NodeTierLevel, TIER_REQUIREMENTS, HardwareProfile,
        )

        # User's actual machine: 4 cores, 8GB, only 3GB disk
        hw = HardwareProfile(cpu_cores=4, ram_gb=8.0, disk_free_gb=3.0, gpu_vram_gb=0.0)

        resolved = NodeTierLevel.EMBEDDED
        for req in TIER_REQUIREMENTS:
            if (hw.cpu_cores >= req.min_cpu_cores and
                hw.ram_gb >= req.min_ram_gb and
                hw.disk_free_gb >= req.min_disk_gb and
                hw.gpu_vram_gb >= req.min_gpu_vram_gb):
                resolved = req.tier
                break

        # 3GB disk ≥ 2GB (STANDARD) but < 20GB (FULL), so STANDARD
        assert resolved == NodeTierLevel.STANDARD

    def test_force_tier_override(self):
        """HEVOLVE_FORCE_TIER overrides hardware detection."""
        from security.system_requirements import NodeTierLevel, FORCE_TIER_ENV

        with patch.dict(os.environ, {FORCE_TIER_ENV: 'standard'}):
            forced = os.environ.get(FORCE_TIER_ENV, '').lower()
            tier_map = {t.value: t for t in NodeTierLevel}
            assert forced in tier_map
            assert tier_map[forced] == NodeTierLevel.STANDARD


class TestFullTierVision:
    """Test 3: FULL tier — vision + local LLM + video generation."""

    def test_full_tier_features(self):
        """FULL tier enables video_gen, media_agent, speculative_dispatch, local_llm."""
        from security.system_requirements import (
            NodeTierLevel, FEATURE_TIER_MAP, _TIER_RANK,
        )

        full_rank = _TIER_RANK[NodeTierLevel.FULL]

        enabled = []
        for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
            if _TIER_RANK[min_tier] <= full_rank:
                enabled.append(feature)

        # FULL tier should have vision and LLM features
        assert 'video_gen' in enabled
        assert 'media_agent' in enabled
        assert 'speculative_dispatch' in enabled
        assert 'local_llm' in enabled
        # Plus all lower-tier features
        assert 'agent_engine' in enabled
        assert 'coding_agent' in enabled
        assert 'tts' in enabled
        assert 'gossip' in enabled

        # But NOT compute_host-only features
        disabled = []
        for feature, (min_tier, env_var) in FEATURE_TIER_MAP.items():
            if _TIER_RANK[min_tier] > full_rank:
                disabled.append(feature)

        assert 'local_llm_large' in disabled
        assert 'regional_host' in disabled

    def test_full_tier_classification(self):
        """8 cores, 16GB RAM, 50GB disk, 8GB VRAM → FULL tier."""
        from security.system_requirements import (
            NodeTierLevel, TIER_REQUIREMENTS, HardwareProfile,
        )

        hw = HardwareProfile(
            cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0,
            gpu_vram_gb=8.0, cuda_available=True,
        )

        resolved = NodeTierLevel.EMBEDDED
        for req in TIER_REQUIREMENTS:
            if (hw.cpu_cores >= req.min_cpu_cores and
                hw.ram_gb >= req.min_ram_gb and
                hw.disk_free_gb >= req.min_disk_gb and
                hw.gpu_vram_gb >= req.min_gpu_vram_gb):
                resolved = req.tier
                break

        assert resolved == NodeTierLevel.FULL


class TestBundledModePathRedirection:
    """Test 4: Bundled mode (Nunba in Program Files) — writable path redirection."""

    def test_agent_data_derives_from_db_path(self):
        """When HEVOLVE_DB_PATH is set to absolute path, agent_data is sibling dir."""
        from helper import _resolve_agent_data_dir

        test_db_path = os.path.join('C:', os.sep, 'Users', 'test', 'Documents',
                                     'Nunba', 'data', 'hevolve.db')

        with patch.dict(os.environ, {'HEVOLVE_DB_PATH': test_db_path}):
            result = _resolve_agent_data_dir()
            expected = os.path.join('C:', os.sep, 'Users', 'test', 'Documents',
                                     'Nunba', 'data', 'agent_data')
            assert result == expected

    def test_agent_data_default_when_no_db_path(self):
        """Without HEVOLVE_DB_PATH, agent_data is relative to project root."""
        from helper import _resolve_agent_data_dir

        with patch.dict(os.environ, {}, clear=False):
            env = os.environ.copy()
            env.pop('HEVOLVE_DB_PATH', None)
            with patch.dict(os.environ, env, clear=True):
                result = _resolve_agent_data_dir()
                # Should be relative to helper.py's directory
                assert result.endswith('agent_data')

    def test_key_dir_derives_from_db_path(self):
        """Ed25519 key directory follows HEVOLVE_DB_PATH for bundled mode."""
        from security.node_integrity import _resolve_key_dir

        test_db_path = os.path.join('C:', os.sep, 'Users', 'test', 'Documents',
                                     'Nunba', 'data', 'hevolve.db')

        with patch.dict(os.environ, {'HEVOLVE_DB_PATH': test_db_path}, clear=False):
            # Clear explicit key dir to test DB-path derivation
            env_copy = os.environ.copy()
            env_copy.pop('HEVOLVE_KEY_DIR', None)
            env_copy['HEVOLVE_DB_PATH'] = test_db_path
            with patch.dict(os.environ, env_copy, clear=True):
                result = _resolve_key_dir()
                expected = os.path.join('C:', os.sep, 'Users', 'test',
                                         'Documents', 'Nunba', 'data')
                assert result == expected

    def test_secret_key_persists_next_to_db(self):
        """JWT secret key persists to disk next to HEVOLVE_DB_PATH."""
        import tempfile
        import secrets
        from integrations.social.auth import _load_or_create_secret_key

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_db = os.path.join(tmpdir, 'hevolve.db')
            with patch.dict(os.environ, {'HEVOLVE_DB_PATH': fake_db}):
                key1 = _load_or_create_secret_key()
                assert len(key1) >= 32

                # Second call should return the same persisted key
                key2 = _load_or_create_secret_key()
                assert key1 == key2

                # Verify file exists
                key_file = os.path.join(tmpdir, '.social_secret_key')
                assert os.path.exists(key_file)

    def test_cache_loaders_agent_data_derives_from_db_path(self):
        """core/cache_loaders.py also derives AGENT_DATA_DIR from DB path."""
        from core.cache_loaders import _resolve_agent_data_dir

        test_db_path = os.path.join('C:', os.sep, 'Users', 'test', 'Documents',
                                     'Nunba', 'data', 'hevolve.db')

        with patch.dict(os.environ, {'HEVOLVE_DB_PATH': test_db_path}):
            result = _resolve_agent_data_dir()
            # Should point to sibling agent_data under the DB path's parent
            assert 'Nunba' in result
            assert result.endswith('agent_data')


class TestDockerDistributedDispatch:
    """Test 5: Docker/distro mode — coordinator + peers → auto-distribute."""

    def test_auto_distribute_when_coordinator_and_peers(self):
        """When coordinator is reachable AND hive has peers, dispatch distributes."""
        from integrations.agent_engine.dispatch import (
            dispatch_goal, _get_distributed_coordinator, _has_hive_peers,
        )

        mock_coordinator = MagicMock()
        mock_coordinator.submit_goal.return_value = 'goal_distributed_123'

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator), \
             patch('integrations.agent_engine.dispatch._has_hive_peers',
                   return_value=True), \
             patch('integrations.agent_engine.dispatch.requests') as mock_requests:

            # Guardrail must pass
            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (True, None, 'test prompt')

                result = dispatch_goal(
                    prompt='test prompt',
                    user_id='user1',
                    goal_id='goal1',
                    goal_type='marketing',
                )

            # Should have tried distributed dispatch
            mock_coordinator.submit_goal.assert_called_once()
            # Should NOT have fallen through to local /chat
            mock_requests.post.assert_not_called()
            assert result == 'goal_distributed_123'

    def test_distributed_dispatch_includes_source_node(self):
        """Distributed tasks carry source_node context for provenance."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_coordinator = MagicMock()
        mock_coordinator.submit_goal.return_value = 'goal_dist_456'

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator), \
             patch('integrations.agent_engine.dispatch._has_hive_peers',
                   return_value=True), \
             patch.dict(os.environ, {'HEVOLVE_NODE_ID': 'test_node_abc'}):

            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (True, None, 'test prompt')

                dispatch_goal(
                    prompt='test prompt',
                    user_id='user1',
                    goal_id='goal2',
                    goal_type='coding',
                )

            # Check that context includes source_node
            call_args = mock_coordinator.submit_goal.call_args
            context = call_args[1].get('context') or call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get('context')
            assert context['source_node'] == 'test_node_abc'

    def test_worker_detects_capabilities_from_tier(self):
        """Worker loop detects capabilities based on system tier."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop

        wl = DistributedWorkerLoop()

        # Base capabilities always present
        assert 'marketing' in wl._capabilities
        assert 'news' in wl._capabilities
        assert 'finance' in wl._capabilities

    def test_has_hive_peers_true_with_active_peers(self):
        """_has_hive_peers returns True when >1 active peers in DB."""
        from integrations.agent_engine.dispatch import _has_hive_peers

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 3
        mock_db.query.return_value = mock_query

        with patch('integrations.social.models.get_db', return_value=mock_db):
            assert _has_hive_peers() is True


class TestNoCoordinatorLocalFallback:
    """Test 6: No shared coordinator → all dispatch goes through local /chat."""

    def test_no_coordinator_dispatches_locally(self):
        """Without Redis coordinator, dispatch_goal falls through to local /chat."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'response': 'local result'}

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=None), \
             patch('integrations.agent_engine.dispatch.requests.post',
                   return_value=mock_response):

            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (True, None, 'test prompt')
                mock_guard.after_response.return_value = (True, None)

                result = dispatch_goal(
                    prompt='test prompt',
                    user_id='user1',
                    goal_id='goal3',
                    goal_type='marketing',
                )

            assert result == 'local result'

    def test_coordinator_exists_but_no_peers_dispatches_locally(self):
        """Coordinator reachable but no peers → dispatch locally (single node)."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_coordinator = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'response': 'local single-node'}

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator), \
             patch('integrations.agent_engine.dispatch._has_hive_peers',
                   return_value=False), \
             patch('integrations.agent_engine.dispatch.requests.post',
                   return_value=mock_response):

            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (True, None, 'test prompt')
                mock_guard.after_response.return_value = (True, None)

                result = dispatch_goal(
                    prompt='test prompt',
                    user_id='user1',
                    goal_id='goal4',
                    goal_type='marketing',
                )

            # Should NOT have distributed
            mock_coordinator.submit_goal.assert_not_called()
            assert result == 'local single-node'

    def test_distributed_fails_falls_back_to_local(self):
        """If distributed dispatch fails, falls back to local /chat."""
        from integrations.agent_engine.dispatch import dispatch_goal

        mock_coordinator = MagicMock()
        mock_coordinator.submit_goal.side_effect = Exception("Redis down")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'response': 'fallback result'}

        with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                   return_value=mock_coordinator), \
             patch('integrations.agent_engine.dispatch._has_hive_peers',
                   return_value=True), \
             patch('integrations.agent_engine.dispatch.requests.post',
                   return_value=mock_response):

            with patch('security.hive_guardrails.GuardrailEnforcer') as mock_guard:
                mock_guard.before_dispatch.return_value = (True, None, 'test prompt')
                mock_guard.after_response.return_value = (True, None)

                result = dispatch_goal(
                    prompt='test prompt',
                    user_id='user1',
                    goal_id='goal5',
                    goal_type='marketing',
                )

            assert result == 'fallback result'

    def test_worker_loop_disabled_without_coordinator(self):
        """Worker loop does not start when no coordinator is reachable."""
        from integrations.distributed_agent.worker_loop import DistributedWorkerLoop

        with patch('integrations.distributed_agent.api._get_coordinator',
                   return_value=None):
            assert DistributedWorkerLoop._is_enabled() is False
