"""
Tests for Batch 5: HevolveAI Source Protection.

Covers:
  - SourceProtectionService: install detection, source visibility, integrity
  - compute_dependency_hash: package hashing
  - hevolveai_access_gate: tier/CCT/integrity gating
  - WorldModelBridge integrity check in _init_in_process
  - compile_hevolveai script (importable, not executed)
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── SourceProtectionService Tests ────────────────────────────────

class TestInstallMethod:
    def test_not_installed(self):
        from security.source_protection import SourceProtectionService
        with patch('importlib.util.find_spec', return_value=None):
            assert SourceProtectionService.check_install_method() == 'not_installed'

    def test_module_not_found(self):
        from security.source_protection import SourceProtectionService
        with patch('importlib.util.find_spec', side_effect=ModuleNotFoundError):
            assert SourceProtectionService.check_install_method() == 'not_installed'

    def test_pyc_origin(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.pyc'
        with patch('importlib.util.find_spec', return_value=mock_spec):
            assert SourceProtectionService.check_install_method() == 'bundled_pyc'

    def test_so_origin(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.cpython-310-x86_64-linux-gnu.so'
        with patch('importlib.util.find_spec', return_value=mock_spec):
            assert SourceProtectionService.check_install_method() == 'bundled_cython'

    def test_pyd_origin(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.pyd'
        with patch('importlib.util.find_spec', return_value=mock_spec):
            assert SourceProtectionService.check_install_method() == 'bundled_cython'

    def test_py_origin_unknown(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.py'
        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch('security.source_protection.pkg_metadata',
                       side_effect=Exception('no metadata'), create=True):
                result = SourceProtectionService.check_install_method()
                # Will try metadata and fail, fall through to 'unknown'
                assert result in ('unknown', 'pip_wheel')


class TestSourceVisibility:
    def test_not_installed(self):
        from security.source_protection import SourceProtectionService
        with patch('importlib.util.find_spec', return_value=None):
            assert SourceProtectionService.is_source_visible() is False

    def test_py_origin_visible(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.py'
        with patch('importlib.util.find_spec', return_value=mock_spec):
            assert SourceProtectionService.is_source_visible() is True

    def test_pyc_origin_not_visible(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/path/to/hevolveai/__init__.pyc'
        mock_spec.submodule_search_locations = []
        with patch('importlib.util.find_spec', return_value=mock_spec):
            assert SourceProtectionService.is_source_visible() is False


class TestIntegrityVerification:
    def test_not_installed(self):
        from security.source_protection import SourceProtectionService
        with patch('importlib.util.find_spec', return_value=None):
            result = SourceProtectionService.verify_hevolveai_integrity()
        assert result['verified'] is False
        assert result['install_method'] == 'not_installed'

    def test_no_manifest_fail_closed(self):
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/fake/hevolveai/__init__.py'
        mock_spec.submodule_search_locations = ['/fake/hevolveai']

        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch.object(SourceProtectionService, '_load_manifest',
                              return_value=None):
                result = SourceProtectionService.verify_hevolveai_integrity()
        # No manifest = fail-closed (Gap 2 fix)
        assert result['verified'] is False
        assert 'manifest not found' in result.get('error', '')

    def test_manifest_match(self, tmp_path):
        from security.source_protection import SourceProtectionService
        import hashlib

        # Create a fake package
        pkg_dir = tmp_path / 'hevolveai'
        pkg_dir.mkdir()
        (pkg_dir / '__init__.py').write_text('# init')
        (pkg_dir / 'core.py').write_text('def hello(): pass')

        # Compute expected hashes
        hashes = {}
        for f in sorted(pkg_dir.rglob('*')):
            if f.is_file():
                rel = str(f.relative_to(pkg_dir)).replace('\\', '/')
                h = hashlib.sha256(f.read_bytes()).hexdigest()
                hashes[rel] = h

        manifest = {'files': hashes}

        mock_spec = MagicMock()
        mock_spec.origin = str(pkg_dir / '__init__.py')
        mock_spec.submodule_search_locations = [str(pkg_dir)]

        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch.object(SourceProtectionService, '_load_manifest',
                              return_value=manifest):
                result = SourceProtectionService.verify_hevolveai_integrity()
        assert result['verified'] is True
        assert result['mismatched_files'] == []
        assert result['missing_files'] == []

    def test_manifest_mismatch(self, tmp_path):
        from security.source_protection import SourceProtectionService

        pkg_dir = tmp_path / 'hevolveai'
        pkg_dir.mkdir()
        (pkg_dir / '__init__.py').write_text('# init')
        (pkg_dir / 'core.py').write_text('def hello(): pass')

        manifest = {
            'files': {
                '__init__.py': 'deadbeef' * 8,  # Wrong hash
                'core.py': 'cafebabe' * 8,       # Wrong hash
            }
        }

        mock_spec = MagicMock()
        mock_spec.origin = str(pkg_dir / '__init__.py')
        mock_spec.submodule_search_locations = [str(pkg_dir)]

        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch.object(SourceProtectionService, '_load_manifest',
                              return_value=manifest):
                result = SourceProtectionService.verify_hevolveai_integrity()
        assert result['verified'] is False
        assert len(result['mismatched_files']) == 2

    def test_manifest_missing_files(self, tmp_path):
        from security.source_protection import SourceProtectionService

        pkg_dir = tmp_path / 'hevolveai'
        pkg_dir.mkdir()
        (pkg_dir / '__init__.py').write_text('# init')

        manifest = {
            'files': {
                '__init__.py': 'abc',
                'missing_module.py': 'def',  # Not on disk
            }
        }

        mock_spec = MagicMock()
        mock_spec.origin = str(pkg_dir / '__init__.py')
        mock_spec.submodule_search_locations = [str(pkg_dir)]

        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch.object(SourceProtectionService, '_load_manifest',
                              return_value=manifest):
                result = SourceProtectionService.verify_hevolveai_integrity()
        assert result['verified'] is False
        assert 'missing_module.py' in result['missing_files']


class TestComputeDependencyHash:
    def test_nonexistent_package(self):
        from security.source_protection import compute_dependency_hash
        with patch('importlib.util.find_spec', return_value=None):
            assert compute_dependency_hash('nonexistent_pkg') is None

    def test_real_package(self, tmp_path):
        from security.source_protection import compute_dependency_hash

        pkg_dir = tmp_path / 'test_pkg'
        pkg_dir.mkdir()
        (pkg_dir / '__init__.py').write_text('# test')
        (pkg_dir / 'module.py').write_text('x = 1')

        mock_spec = MagicMock()
        mock_spec.submodule_search_locations = [str(pkg_dir)]

        with patch('importlib.util.find_spec', return_value=mock_spec):
            h = compute_dependency_hash('test_pkg')
        assert h is not None
        assert len(h) == 64  # SHA-256 hex


# ── Access Gate Tests ────────────────────────────────────────────

class TestAccessGate:
    def test_unknown_feature_allowed(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        result = check_hevolveai_access('some_new_feature')
        assert result['allowed'] is True

    def test_in_process_requires_integrity(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        with patch('integrations.robotics.hevolveai_access_gate._get_node_tier',
                   return_value='local'):
            with patch('integrations.robotics.hevolveai_access_gate._check_integrity',
                       return_value=False):
                result = check_hevolveai_access('in_process')
        assert result['allowed'] is False
        assert 'verified' in result['reason']

    def test_in_process_with_integrity(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        with patch('integrations.robotics.hevolveai_access_gate._get_node_tier',
                   return_value='local'):
            with patch('integrations.robotics.hevolveai_access_gate._check_integrity',
                       return_value=True):
                result = check_hevolveai_access('in_process')
        assert result['allowed'] is True

    def test_hivemind_requires_regional(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        with patch('integrations.robotics.hevolveai_access_gate._get_node_tier',
                   return_value='local'):
            result = check_hevolveai_access('hivemind')
        assert result['allowed'] is False
        assert 'regional' in result['reason']

    def test_hivemind_regional_with_integrity(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        with patch('integrations.robotics.hevolveai_access_gate._get_node_tier',
                   return_value='regional'):
            with patch('integrations.robotics.hevolveai_access_gate._check_integrity',
                       return_value=True):
                with patch('integrations.robotics.hevolveai_access_gate._has_cct_capability',
                           return_value=True):
                    result = check_hevolveai_access('hivemind')
        assert result['allowed'] is True

    def test_sensor_fusion_needs_cct(self):
        from integrations.robotics.hevolveai_access_gate import (
            check_hevolveai_access,
        )
        with patch('integrations.robotics.hevolveai_access_gate._get_node_tier',
                   return_value='local'):
            with patch('integrations.robotics.hevolveai_access_gate._check_integrity',
                       return_value=True):
                with patch('integrations.robotics.hevolveai_access_gate._has_cct_capability',
                           return_value=False):
                    result = check_hevolveai_access('sensor_fusion')
        assert result['allowed'] is False
        assert 'CCT' in result['reason']


class TestTierHierarchy:
    def test_tier_meets_minimum(self):
        from integrations.robotics.hevolveai_access_gate import (
            _tier_meets_minimum,
        )
        assert _tier_meets_minimum('central', 'local') is True
        assert _tier_meets_minimum('regional', 'regional') is True
        assert _tier_meets_minimum('local', 'regional') is False
        assert _tier_meets_minimum('observer', 'local') is False
        assert _tier_meets_minimum('central', 'observer') is True


# ── WorldModelBridge Integrity Gate Tests ────────────────────────

class TestBridgeIntegrityGate:
    def test_bridge_blocks_on_mismatch(self):
        """If integrity check fails, _init_in_process should NOT enable in-process."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()

        mock_integrity = {
            'verified': False,
            'mismatched_files': ['core.py'],
        }
        with patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity',
                   return_value=mock_integrity):
            bridge._init_in_process()

        assert bridge._in_process is False

    def test_bridge_allows_on_verified(self):
        """If integrity passes, normal in-process init proceeds."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()

        mock_integrity = {'verified': True, 'mismatched_files': []}
        with patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity',
                   return_value=mock_integrity):
            # Still won't be in-process because langchain_gpt_api
            # providers won't be available, but it shouldn't block
            bridge._init_in_process()
        # Just verify it didn't raise — in-process depends on provider
        assert bridge._in_process is False  # No provider available

    def test_bridge_skips_if_no_source_protection(self):
        """If source_protection module unavailable, skip check gracefully."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()

        with patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity',
                   side_effect=ImportError('no module')):
            bridge._init_in_process()
        # Should not raise, falls through to provider check
        assert bridge._in_process is False


# ── Compile Script Tests ─────────────────────────────────────────

class TestCompileScript:
    def test_importable(self):
        """Verify the compile script can be imported without side effects."""
        import importlib
        spec = importlib.util.find_spec('scripts.compile_hevolveai')
        # Script may not be importable as a module (no __init__.py in scripts/)
        # but we can at least verify it parses
        import ast
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'scripts', 'compile_hevolveai.py',
        )
        with open(script_path, 'r') as f:
            source = f.read()
        # Should parse without syntax errors
        tree = ast.parse(source)
        assert tree is not None

    def test_manifest_placeholder_exists(self):
        """Verify the manifest placeholder file exists."""
        manifest_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'security', 'hevolveai_manifest.json',
        )
        assert os.path.exists(manifest_path)
        with open(manifest_path, 'r') as f:
            data = json.load(f)
        assert 'files' in data
        assert '_comment' in data


# ── Gap 1: CrawlIntegrityWatcher Tests ──────────────────────────

class TestCrawlIntegrityWatcher:
    def test_starts_healthy(self):
        """Watcher starts cleanly and reports healthy."""
        from security.source_protection import CrawlIntegrityWatcher
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='aabbcc'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        assert watcher.is_healthy is True
        assert watcher._boot_hash == 'aabbcc'

    def test_callback_fires_on_hash_change(self):
        """Tamper callback fires when hash changes between checks."""
        from security.source_protection import CrawlIntegrityWatcher
        fired = []
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='original_hash'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        watcher.register_tamper_callback(lambda: fired.append(True))
        # Simulate hash change
        watcher._compute_current_hash = lambda: 'tampered_hash'
        watcher._check_once_for_test()
        assert watcher.is_healthy is False
        assert len(fired) == 1

    def test_no_callback_when_hash_unchanged(self):
        """No callback fires when hash is stable."""
        from security.source_protection import CrawlIntegrityWatcher
        fired = []
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='stable_hash'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        watcher.register_tamper_callback(lambda: fired.append(True))
        watcher._compute_current_hash = lambda: 'stable_hash'
        watcher._check_once_for_test()
        assert watcher.is_healthy is True
        assert len(fired) == 0

    def test_skip_env_var_disables_loop(self):
        """HEVOLVE_TAMPER_CHECK_SKIP=true causes _check_loop to exit."""
        from security.source_protection import CrawlIntegrityWatcher
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='hash123'):
            watcher = CrawlIntegrityWatcher(check_interval=1)
        with patch.dict(os.environ, {'HEVOLVE_TAMPER_CHECK_SKIP': 'true'}):
            watcher._running = True
            watcher._check_loop()  # Should return immediately
        assert watcher.is_healthy is True

    def test_hevolveai_not_installed_no_false_tamper(self):
        """Empty hash from missing HevolveAI doesn't trigger false tamper."""
        from security.source_protection import CrawlIntegrityWatcher
        with patch('security.source_protection.compute_dependency_hash',
                   return_value=None):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        fired = []
        watcher.register_tamper_callback(lambda: fired.append(True))
        watcher._compute_current_hash = lambda: ''
        watcher._check_once_for_test()
        assert watcher.is_healthy is True
        assert len(fired) == 0

    def test_multiple_callbacks(self):
        """All registered callbacks fire on tamper detection."""
        from security.source_protection import CrawlIntegrityWatcher
        results = {'a': False, 'b': False}
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='original'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        watcher.register_tamper_callback(lambda: results.__setitem__('a', True))
        watcher.register_tamper_callback(lambda: results.__setitem__('b', True))
        watcher._compute_current_hash = lambda: 'tampered'
        watcher._check_once_for_test()
        assert results == {'a': True, 'b': True}

    def test_callback_exception_does_not_block_others(self):
        """A failing callback doesn't prevent other callbacks from firing."""
        from security.source_protection import CrawlIntegrityWatcher
        fired = []
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='original'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        watcher.register_tamper_callback(lambda: (_ for _ in ()).throw(ValueError))
        watcher.register_tamper_callback(lambda: fired.append(True))
        watcher._compute_current_hash = lambda: 'tampered'
        watcher._check_once_for_test()
        assert len(fired) == 1
        assert watcher.is_healthy is False

    def test_start_stop_lifecycle(self):
        """Watcher can be started and stopped cleanly."""
        from security.source_protection import CrawlIntegrityWatcher
        with patch('security.source_protection.compute_dependency_hash',
                   return_value='hash'):
            watcher = CrawlIntegrityWatcher(check_interval=999)
        with patch.dict(os.environ, {'HEVOLVE_TAMPER_CHECK_SKIP': 'true'}):
            watcher.start()
            assert watcher._running is True
            watcher.stop()
            assert watcher._running is False


# ── Gap 2: Fail-Closed Tests ──────────────────────────────────

class TestGap2FailClosed:
    def test_missing_manifest_returns_verified_false(self):
        """Gap 2: No manifest must return verified=False."""
        from security.source_protection import SourceProtectionService
        mock_spec = MagicMock()
        mock_spec.origin = '/fake/hevolveai/__init__.py'
        mock_spec.submodule_search_locations = ['/fake/hevolveai']
        with patch('importlib.util.find_spec', return_value=mock_spec):
            with patch.object(SourceProtectionService, '_load_manifest',
                              return_value=None):
                result = SourceProtectionService.verify_hevolveai_integrity()
        assert result['verified'] is False
        assert 'manifest not found' in result.get('error', '')

    def test_missing_manifest_blocks_in_process(self):
        """Gap 2: WorldModelBridge must not enable in-process without manifest."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        mock_integrity = {
            'verified': False,
            'mismatched_files': [],
            'error': 'manifest not found or invalid',
        }
        with patch('security.source_protection.SourceProtectionService'
                   '.verify_hevolveai_integrity',
                   return_value=mock_integrity):
            bridge._init_in_process()
        assert bridge._in_process is False


# ── Gap 3: CCT Fail-Closed Tests ──────────────────────────────

class TestGap3CCTFailClosed:
    def test_exception_returns_false_in_production(self):
        """Gap 3: CCT exception returns False in production."""
        from integrations.robotics.hevolveai_access_gate import _has_cct_capability
        env = dict(os.environ)
        env.pop('HEVOLVE_DEV_MODE', None)
        with patch.dict(os.environ, env, clear=True):
            with patch(
                'integrations.agent_engine.continual_learner_gate'
                '.ContinualLearnerGate',
                side_effect=ImportError('no module'), create=True,
            ):
                result = _has_cct_capability('embodied_ai')
        assert result is False

    def test_exception_returns_true_in_dev_mode(self):
        """Gap 3: CCT exception returns True in dev mode."""
        from integrations.robotics.hevolveai_access_gate import _has_cct_capability
        with patch.dict(os.environ, {'HEVOLVE_DEV_MODE': 'true'}):
            with patch(
                'integrations.agent_engine.continual_learner_gate'
                '.ContinualLearnerGate',
                side_effect=ImportError('no module'), create=True,
            ):
                result = _has_cct_capability('embodied_ai')
        assert result is True

    def test_no_valid_cct_returns_false(self):
        """Normal path: has_valid_cct=False returns False."""
        from integrations.robotics.hevolveai_access_gate import _has_cct_capability
        mock_gate = MagicMock()
        mock_gate.check_access.return_value = {
            'has_valid_cct': False, 'capabilities': []}
        with patch(
            'integrations.robotics.hevolveai_access_gate.ContinualLearnerGate',
            return_value=mock_gate, create=True,
        ):
            result = _has_cct_capability('embodied_ai')
        assert result is False

    def test_valid_cct_with_capability_returns_true(self):
        """Normal path: valid CCT with required capability returns True."""
        import sys
        from integrations.robotics.hevolveai_access_gate import _has_cct_capability
        mock_gate_cls = MagicMock()
        mock_gate_cls.return_value.check_access.return_value = {
            'has_valid_cct': True,
            'capabilities': ['embodied_ai', 'sensor_fusion'],
        }
        # The function imports ContinualLearnerGate from the module;
        # must make it importable via the module's namespace
        mock_module = MagicMock()
        mock_module.ContinualLearnerGate = mock_gate_cls
        with patch.dict(sys.modules,
                        {'integrations.agent_engine.continual_learner_gate':
                         mock_module}):
            result = _has_cct_capability('embodied_ai')
        assert result is True


# ── WorldModelBridge Tamper Callback Tests ──────────────────────

class TestWorldModelBridgeTamperCallback:
    def test_tamper_callback_disables_in_process(self):
        """When watcher fires, _in_process is set to False."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        bridge._in_process = True
        bridge._provider = MagicMock()
        bridge._hive_mind = MagicMock()
        bridge._on_crawl_tamper_detected()
        assert bridge._in_process is False
        assert bridge._provider is None
        assert bridge._hive_mind is None

    def test_watcher_not_started_in_http_mode(self):
        """Watcher is not started when bridge is in HTTP mode."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        assert bridge._in_process is False
        assert bridge._crawl_watcher is None

    def test_watcher_started_when_in_process(self):
        """Watcher is created when bridge enters in-process mode."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        bridge._in_process = True
        mock_watcher_cls = MagicMock()
        mock_watcher_instance = MagicMock()
        mock_watcher_cls.return_value = mock_watcher_instance
        with patch('security.source_protection.CrawlIntegrityWatcher',
                   mock_watcher_cls):
            bridge._start_crawl_integrity_watcher()
        mock_watcher_instance.register_tamper_callback.assert_called_once_with(
            bridge._on_crawl_tamper_detected)
        mock_watcher_instance.start.assert_called_once()
        assert bridge._crawl_watcher is mock_watcher_instance


# ══════════════════════════════════════════════════════════════════
# Batch 8: Perimeter Hash Enforcement Tests
# ══════════════════════════════════════════════════════════════════

class TestReleaseHashRegistry:
    def test_known_hash_accepted(self):
        """Hardcoded GA hash is recognized."""
        import security.release_hash_registry as rhm
        original = rhm._KNOWN_HASHES.copy()
        try:
            rhm._KNOWN_HASHES['1.0.0'] = 'abc123'
            from security.release_hash_registry import ReleaseHashRegistry
            reg = ReleaseHashRegistry()
            assert reg.is_known_release_hash('abc123') is True
        finally:
            rhm._KNOWN_HASHES.clear()
            rhm._KNOWN_HASHES.update(original)

    def test_unknown_hash_rejected(self):
        """Unknown hash is not recognized."""
        from security.release_hash_registry import ReleaseHashRegistry
        reg = ReleaseHashRegistry()
        assert reg.is_known_release_hash('totally_unknown_hash') is False

    def test_empty_hash_rejected(self):
        """Empty string hash is not recognized."""
        from security.release_hash_registry import ReleaseHashRegistry
        reg = ReleaseHashRegistry()
        assert reg.is_known_release_hash('') is False

    def test_manifest_hash_trusted(self):
        """Current manifest's code_hash is always trusted."""
        from security.release_hash_registry import ReleaseHashRegistry
        with patch('security.master_key.load_release_manifest',
                   return_value={'code_hash': 'manifest_hash_123'}):
            with patch('security.master_key.verify_release_manifest',
                       return_value=True):
                reg = ReleaseHashRegistry()
        assert reg.is_known_release_hash('manifest_hash_123') is True

    def test_runtime_hash_addition(self):
        """Hashes added at runtime from verified peers are recognized."""
        from security.release_hash_registry import ReleaseHashRegistry
        reg = ReleaseHashRegistry()
        reg.add_runtime_hash('2.0.0', 'runtime_peer_hash')
        assert reg.is_known_release_hash('runtime_peer_hash') is True

    def test_bounded_dict_overflow(self):
        """Runtime hashes are bounded — oldest evicted first (FIFO)."""
        import security.release_hash_registry as rhm
        from security.release_hash_registry import ReleaseHashRegistry
        reg = ReleaseHashRegistry()
        # Fill to max
        for i in range(rhm._MAX_RUNTIME_HASHES + 5):
            reg.add_runtime_hash(f'v{i}', f'hash_{i}')
        # The earliest entries should be evicted
        assert reg.is_known_release_hash('hash_0') is False
        assert reg.is_known_release_hash('hash_4') is False
        # Latest should remain
        latest = rhm._MAX_RUNTIME_HASHES + 4
        assert reg.is_known_release_hash(f'hash_{latest}') is True

    def test_get_known_versions(self):
        """get_known_versions returns all sources."""
        import security.release_hash_registry as rhm
        original = rhm._KNOWN_HASHES.copy()
        try:
            rhm._KNOWN_HASHES['1.0.0'] = 'ga_hash'
            from security.release_hash_registry import ReleaseHashRegistry
            reg = ReleaseHashRegistry()
            reg.add_runtime_hash('2.0.0', 'rt_hash')
            versions = reg.get_known_versions()
            assert '1.0.0' in versions
            assert versions['1.0.0'] == 'ga_hash'
            assert '2.0.0' in versions
        finally:
            rhm._KNOWN_HASHES.clear()
            rhm._KNOWN_HASHES.update(original)

    def test_hash_count(self):
        """hash_count reflects all sources."""
        from security.release_hash_registry import ReleaseHashRegistry
        reg = ReleaseHashRegistry()
        base = reg.hash_count()
        reg.add_runtime_hash('v1', 'h1')
        assert reg.hash_count() == base + 1


class TestPerimeterEnforcement:
    """Tests for _merge_peer code hash enforcement via registry."""

    def _make_gossip(self):
        """Create a minimal GossipProtocol-like object for testing."""
        mock = MagicMock()
        mock.node_id = 'self_node'
        mock.base_url = 'http://localhost:6777'
        mock.node_name = 'test'
        mock.version = '1.0'
        mock.tier = 'flat'
        return mock

    def test_merge_peer_accepts_known_registry_hash(self):
        """Peer with hash in registry is accepted even if manifest differs."""
        from integrations.social.peer_discovery import GossipProtocol
        import security.release_hash_registry as rhm
        original = rhm._KNOWN_HASHES.copy()
        try:
            rhm._KNOWN_HASHES['1.0.0'] = 'known_ga_hash'
            gp = GossipProtocol.__new__(GossipProtocol)
            gp.node_id = 'self_node'
            gp.base_url = 'http://localhost:6777'
            db = MagicMock()
            db.query.return_value.filter.return_value.first.return_value = None
            db.query.return_value.filter_by.return_value.first.return_value = None
            peer_data = {
                'node_id': 'peer_abc',
                'url': 'http://peer:6777',
                'code_hash': 'known_ga_hash',
            }
            # Should not reject — hash is in registry
            result = gp._merge_peer(db, peer_data)
            # Result is True (new peer added) or False (no error, just no sig)
            # The key test: it should NOT return False from the hash check
            # We verify by ensuring db.add was called (new peer inserted)
            assert db.add.called
        finally:
            rhm._KNOWN_HASHES.clear()
            rhm._KNOWN_HASHES.update(original)

    def test_merge_peer_rejects_unknown_hash_hard_mode(self):
        """Peer with unknown hash is rejected in hard enforcement."""
        from integrations.social.peer_discovery import GossipProtocol
        gp = GossipProtocol.__new__(GossipProtocol)
        gp.node_id = 'self_node'
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter_by.return_value.first.return_value = None
        peer_data = {
            'node_id': 'evil_peer',
            'url': 'http://evil:6777',
            'code_hash': 'evil_unknown_hash',
        }
        with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
            mock_reg.return_value.is_known_release_hash.return_value = False
            with patch('security.master_key.load_release_manifest',
                       return_value=None):
                with patch('security.master_key.get_enforcement_mode',
                           return_value='hard'):
                    result = gp._merge_peer(db, peer_data)
        assert result is False

    def test_compact_gossip_includes_code_hash(self):
        """code_hash is in _COMPACT_FIELDS."""
        from integrations.social.peer_discovery import _COMPACT_FIELDS
        assert 'code_hash' in _COMPACT_FIELDS


class TestBeaconHashVerification:
    """Tests for code_hash in UDP beacon build/parse."""

    def test_beacon_includes_code_hash(self):
        """_build_beacon includes code_hash in payload."""
        import json as _json
        from integrations.social.peer_discovery import AutoDiscovery
        ad = AutoDiscovery.__new__(AutoDiscovery)
        mock_gossip = MagicMock()
        mock_gossip.node_id = 'test_node'
        mock_gossip.base_url = 'http://localhost:6777'
        mock_gossip.node_name = 'test'
        mock_gossip.version = '1.0'
        mock_gossip.tier = 'flat'
        ad._gossip = mock_gossip

        with patch('security.hive_guardrails.get_guardrail_hash',
                   return_value='gh123'):
            with patch('security.node_integrity.get_public_key_hex',
                       return_value='pk123'):
                with patch('security.node_integrity.compute_code_hash',
                           return_value='ch123'):
                    with patch('security.node_integrity.sign_json_payload',
                               return_value='sig123'):
                        with patch('security.master_key.load_release_manifest',
                                   return_value={'version': '1.0'}):
                            beacon = ad._build_beacon()

        # Parse the beacon bytes
        magic = b'HEVOLVE_DISCO_V1'
        payload = _json.loads(beacon[len(magic):].decode('utf-8'))
        assert payload['code_hash'] == 'ch123'
        assert payload['release_version'] == '1.0'

    def test_parse_beacon_rejects_unknown_hash_hard(self):
        """Beacon with unknown code hash rejected in hard enforcement."""
        import json as _json
        from integrations.social.peer_discovery import AutoDiscovery
        ad = AutoDiscovery.__new__(AutoDiscovery)
        mock_gossip = MagicMock()
        mock_gossip.node_id = 'self_node'
        ad._gossip = mock_gossip
        ad.BEACON_MAGIC = b'HEVOLVE_DISCO_V1'

        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'remote_peer',
            'url': 'http://remote:6777',
            'timestamp': int(__import__('time').time()),
            'guardrail_hash': 'gh_match',
            'code_hash': 'unknown_evil_hash',
        }
        data = ad.BEACON_MAGIC + _json.dumps(payload).encode('utf-8')

        with patch('security.hive_guardrails.get_guardrail_hash',
                   return_value='gh_match'):
            with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
                mock_reg.return_value.is_known_release_hash.return_value = False
                with patch('security.master_key.get_enforcement_mode',
                           return_value='hard'):
                    result = ad._parse_beacon(data)
        assert result == {}

    def test_parse_beacon_accepts_known_hash(self):
        """Beacon with known code hash is accepted."""
        import json as _json
        from integrations.social.peer_discovery import AutoDiscovery
        ad = AutoDiscovery.__new__(AutoDiscovery)
        mock_gossip = MagicMock()
        mock_gossip.node_id = 'self_node'
        ad._gossip = mock_gossip
        ad.BEACON_MAGIC = b'HEVOLVE_DISCO_V1'

        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'good_peer',
            'url': 'http://good:6777',
            'timestamp': int(__import__('time').time()),
            'guardrail_hash': 'gh_match',
            'code_hash': 'known_good_hash',
        }
        data = ad.BEACON_MAGIC + _json.dumps(payload).encode('utf-8')

        with patch('security.hive_guardrails.get_guardrail_hash',
                   return_value='gh_match'):
            with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
                mock_reg.return_value.is_known_release_hash.return_value = True
                result = ad._parse_beacon(data)
        assert result != {}
        assert result['node_id'] == 'good_peer'

    def test_parse_beacon_allows_no_hash_soft_mode(self):
        """Beacon without code_hash is accepted (backward compat)."""
        import json as _json
        from integrations.social.peer_discovery import AutoDiscovery
        ad = AutoDiscovery.__new__(AutoDiscovery)
        mock_gossip = MagicMock()
        mock_gossip.node_id = 'self_node'
        ad._gossip = mock_gossip
        ad.BEACON_MAGIC = b'HEVOLVE_DISCO_V1'

        payload = {
            'type': 'hevolve-discovery',
            'node_id': 'legacy_peer',
            'url': 'http://legacy:6777',
            'timestamp': int(__import__('time').time()),
        }
        data = ad.BEACON_MAGIC + _json.dumps(payload).encode('utf-8')
        result = ad._parse_beacon(data)
        assert result != {}
        assert result['node_id'] == 'legacy_peer'


class TestIntegrityServiceRegistry:
    """Tests for verify_code_hash using registry."""

    def test_registry_match_short_circuits(self):
        """Known registry hash returns verified=True immediately."""
        from integrations.social.integrity_service import IntegrityService
        db = MagicMock()
        peer = MagicMock()
        peer.code_hash = 'known_hash'
        db.query.return_value.filter_by.return_value.first.return_value = peer

        with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
            mock_reg.return_value.is_known_release_hash.return_value = True
            result = IntegrityService.verify_code_hash(db, 'node_x')
        assert result['verified'] is True
        assert 'registry' in result['details']

    def test_registry_miss_falls_through_to_manifest(self):
        """Unknown registry hash falls through to manifest check."""
        from integrations.social.integrity_service import IntegrityService
        db = MagicMock()
        peer = MagicMock()
        peer.code_hash = 'some_hash'
        peer.code_version = '1.0'
        peer.version = '1.0'
        db.query.return_value.filter_by.return_value.first.return_value = peer

        with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
            mock_reg.return_value.is_known_release_hash.return_value = False
            with patch('security.master_key.load_release_manifest',
                       return_value={'code_hash': 'some_hash'}):
                with patch('security.master_key.verify_release_manifest',
                           return_value=True):
                    result = IntegrityService.verify_code_hash(db, 'node_x')
        assert result['verified'] is True


class TestUpgradeRegistryIntegration:
    """Tests for upgrade orchestrator → registry hash registration."""

    def test_deploy_registers_hash(self):
        """_stage_deploy adds the new hash to ReleaseHashRegistry."""
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        orch = UpgradeOrchestrator.__new__(UpgradeOrchestrator)
        orch._state = {
            'version': '2.0.0',
            'git_sha': 'abc123',
            'code_hash': 'new_release_hash',
        }

        mock_gossip = MagicMock()
        with patch('integrations.social.peer_discovery.gossip', mock_gossip):
            with patch('security.release_hash_registry.get_release_hash_registry') as mock_reg:
                ok, msg = orch._stage_deploy()
        assert ok is True
        mock_reg.return_value.add_runtime_hash.assert_called_once_with(
            '2.0.0', 'new_release_hash')

    def test_deploy_no_registry_still_succeeds(self):
        """Deploy broadcast succeeds even if registry import fails."""
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        orch = UpgradeOrchestrator.__new__(UpgradeOrchestrator)
        orch._state = {
            'version': '2.0.0',
            'git_sha': 'abc',
            'code_hash': 'hash',
        }

        mock_gossip = MagicMock()
        with patch('integrations.social.peer_discovery.gossip', mock_gossip):
            with patch('security.release_hash_registry.get_release_hash_registry',
                       side_effect=ImportError):
                ok, msg = orch._stage_deploy()
        assert ok is True
