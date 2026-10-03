"""
Tests for Coding Agent Tool Backends, Installer, and Benchmark Tracker.

Tests the subprocess wrapper pattern, tool detection, benchmark recording,
and hive delta export/import — all without requiring actual tools installed.
"""
import os
import sys
import json
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock


# ─── Installer Tests ───

class TestInstaller:
    def test_detect_installed_returns_dict(self):
        from integrations.coding_agent.installer import detect_installed
        result = detect_installed()
        assert isinstance(result, dict)
        assert 'kilocode' in result
        assert 'claude_code' in result
        assert 'opencode' in result

    def test_get_tool_info_structure(self):
        from integrations.coding_agent.installer import get_tool_info
        info = get_tool_info()
        for name in ('kilocode', 'claude_code', 'opencode'):
            assert name in info
            assert 'installed' in info[name]
            assert 'binary' in info[name]
            assert 'package' in info[name]
            assert 'license' in info[name]

    def test_tool_registry_licenses(self):
        from integrations.coding_agent.installer import TOOL_REGISTRY
        assert TOOL_REGISTRY['kilocode'][2] == 'Apache-2.0'
        assert TOOL_REGISTRY['claude_code'][2] == 'Proprietary'
        assert TOOL_REGISTRY['opencode'][2] == 'MIT'

    def test_install_unknown_tool(self):
        from integrations.coding_agent.installer import install
        result = install('nonexistent_tool')
        assert result['success'] is False
        assert 'Unknown tool' in result['error']

    def test_pi_and_hermes_in_registry(self):
        from integrations.coding_agent.installer import TOOL_REGISTRY
        assert TOOL_REGISTRY['pi'] == ('pi', '@mariozechner/pi-coding-agent', 'MIT')
        assert TOOL_REGISTRY['hermes'][0] == 'hermes'
        assert TOOL_REGISTRY['hermes'][2] == 'MIT'

    @patch('shutil.which', return_value=None)
    def test_hermes_install_is_never_a_piped_remote_script(self, mock_which):
        """Hermes ships only as an official install script; HARTOS hands the
        user the command instead of running a remote script itself."""
        from integrations.coding_agent import installer
        with patch.object(installer.subprocess, 'run') as run:
            result = installer.install_tool('hermes')
        run.assert_not_called()
        assert result['success'] is False
        assert 'hermes-agent.nousresearch.com' in result['error']

    def test_npm_install_and_version_launch_the_resolved_path(self):
        """Same .cmd-shim trap as execute(): npm and every npm-installed
        binary must be launched by the path shutil.which resolved."""
        from integrations.coding_agent import installer

        def which(name):
            return {'npm': r'C:\nodejs\npm.CMD',
                    'pi': r'C:\npm\pi.CMD'}.get(name) if name != 'pi' or which.installed else None
        which.installed = False
        with patch.object(installer.shutil, 'which', side_effect=which), \
             patch.object(installer.subprocess, 'run') as run:
            run.return_value = MagicMock(stdout='0.73.1', stderr='', returncode=0)
            installer.install_tool('pi')
            assert run.call_args[0][0] == [
                r'C:\nodejs\npm.CMD', 'install', '-g', '@mariozechner/pi-coding-agent']
            which.installed = True
            versions = installer.get_versions()
        assert versions['pi'] == '0.73.1'
        assert [r'C:\npm\pi.CMD', '--version'] in [c[0][0] for c in run.call_args_list]

    def test_no_tools_message_names_every_registered_tool(self):
        from integrations.coding_agent.installer import TOOL_REGISTRY
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        with patch('integrations.coding_agent.tool_router.get_available_backends',
                   return_value={}):
            result = CodingAgentOrchestrator()._execute_local(
                'task', 'feature', '', 'u1', '', '')
        for name in TOOL_REGISTRY:
            assert name in result['error']

    @patch('shutil.which', return_value=None)
    def test_install_without_npm(self, mock_which):
        from integrations.coding_agent.installer import install
        result = install('kilocode')
        assert result['success'] is False


# ─── Backend Tests ───

class TestToolBackends:
    def test_backend_registry(self):
        from integrations.coding_agent.tool_backends import BACKENDS
        assert 'kilocode' in BACKENDS
        assert 'claude_code' in BACKENDS
        assert 'opencode' in BACKENDS

    def test_backend_capabilities(self):
        from integrations.coding_agent.tool_backends import get_all_backends
        backends = get_all_backends()
        for name, backend in backends.items():
            caps = backend.get_capabilities()
            assert 'name' in caps
            assert 'binary' in caps
            assert 'installed' in caps
            assert 'strengths' in caps
            assert isinstance(caps['strengths'], list)

    def test_kilocode_command_build(self):
        from integrations.coding_agent.tool_backends import KiloCodeBackend
        backend = KiloCodeBackend()
        cmd = backend.build_command('review this code', {'model': 'gpt-4.1'})
        assert cmd[0] == 'kilocode'
        assert '--auto' in cmd
        assert '--json-io' in cmd
        assert '--model' in cmd
        assert 'gpt-4.1' in cmd

    def test_claude_code_command_build(self):
        from integrations.coding_agent.tool_backends import ClaudeCodeBackend
        backend = ClaudeCodeBackend()
        cmd = backend.build_command('fix this bug')
        assert cmd[0] == 'claude'
        assert '-p' in cmd
        assert '--output-format' in cmd
        assert 'json' in cmd

    def test_opencode_command_build(self):
        from integrations.coding_agent.tool_backends import OpenCodeBackend
        backend = OpenCodeBackend()
        cmd = backend.build_command('refactor module')
        assert cmd[0] == 'opencode'
        assert '-p' in cmd
        assert '-f' in cmd

    def test_execute_not_installed(self):
        from integrations.coding_agent.tool_backends import KiloCodeBackend
        backend = KiloCodeBackend()
        with patch.object(backend, 'is_installed', return_value=False):
            result = backend.execute('test task')
            assert result['success'] is False
            assert 'not installed' in result['error']

    def test_parse_output_valid_json(self):
        from integrations.coding_agent.tool_backends import KiloCodeBackend
        backend = KiloCodeBackend()
        result = backend.parse_output('{"result": "done"}', '', 0)
        assert result['success'] is True
        assert result['output'] == 'done'

    def test_parse_output_invalid_json(self):
        from integrations.coding_agent.tool_backends import ClaudeCodeBackend
        backend = ClaudeCodeBackend()
        result = backend.parse_output('plain text output', '', 0)
        assert result['success'] is True
        assert result['output'] == 'plain text output'

    def test_pi_command_build(self):
        from integrations.coding_agent.tool_backends import PiBackend
        cmd = PiBackend().build_command('refactor module', {'model': 'anthropic/claude-sonnet-4'})
        assert cmd == ['pi', '-p', 'refactor module', '--model', 'anthropic/claude-sonnet-4']

    def test_hermes_command_build(self):
        from integrations.coding_agent.tool_backends import HermesBackend
        cmd = HermesBackend().build_command('fix the failing test')
        # -Q: only the final response on stdout (no banner/spinner/previews)
        assert cmd == ['hermes', 'chat', '-Q', '-q', 'fix the failing test']

    def test_plain_text_backends_report_stdout(self):
        from integrations.coding_agent.tool_backends import HermesBackend, PiBackend
        for backend in (PiBackend(), HermesBackend()):
            ok = backend.parse_output('  done: 3 files changed\n', '', 0)
            assert ok == {'success': True, 'output': 'done: 3 files changed'}
            bad = backend.parse_output('', 'no API key', 1)
            assert bad == {'success': False, 'output': 'no API key'}

    def test_pi_and_hermes_are_selectable(self):
        """Registered, so the router's user override can pick them."""
        from integrations.coding_agent.tool_backends import BACKENDS, HermesBackend, PiBackend
        from integrations.coding_agent.tool_router import CodingToolRouter
        assert BACKENDS['pi'] is PiBackend
        assert BACKENDS['hermes'] is HermesBackend
        with patch('integrations.coding_agent.tool_router.get_available_backends',
                   return_value={'pi': PiBackend(), 'hermes': HermesBackend()}):
            assert CodingToolRouter().route('t', 'feature', user_override='hermes').name == 'hermes'
            assert CodingToolRouter().route('t', 'feature', user_override='pi').name == 'pi'

    def test_execute_launches_the_resolved_path(self):
        """npm installs pi/kilocode/opencode as .cmd shims; on Windows a bare
        name in the argv list is FileNotFoundError (CreateProcess does not use
        PATHEXT).  execute() must launch what shutil.which resolved."""
        from integrations.coding_agent import tool_backends
        backend = tool_backends.PiBackend()
        shim = r'C:\Users\x\AppData\Roaming\npm\pi.CMD'
        with patch.object(tool_backends.shutil, 'which', return_value=shim), \
             patch.object(tool_backends.subprocess, 'run') as run:
            run.return_value = MagicMock(stdout='ok', stderr='', returncode=0)
            result = backend.execute('task')
        assert run.call_args[0][0][0] == shim
        assert result['success'] is True and result['tool'] == 'pi'

    def test_env_passthrough(self):
        from integrations.coding_agent.tool_backends import KiloCodeBackend
        backend = KiloCodeBackend()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            env = backend.get_env()
            assert env.get('OPENAI_API_KEY') == 'test-key'


# ─── Benchmark Tracker Tests ───

class TestBenchmarkTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        from integrations.coding_agent.benchmark_tracker import BenchmarkTracker
        db_path = str(tmp_path / 'test_benchmarks.db')
        return BenchmarkTracker(db_path=db_path)

    def test_record_and_get_summary(self, tracker):
        tracker.record('code_review', 'claude_code', 5.0, True, 'gpt-4.1', 'user1')
        tracker.record('code_review', 'claude_code', 3.0, True, 'gpt-4.1', 'user1')
        summary = tracker.get_summary()
        assert summary['total_benchmarks'] == 2
        assert len(summary['by_tool']) == 1
        assert summary['by_tool'][0]['tool'] == 'claude_code'

    def test_get_best_tool_insufficient_data(self, tracker):
        # Less than MIN_SAMPLES entries
        tracker.record('feature', 'kilocode', 10.0, True)
        result = tracker.get_best_tool('feature')
        assert result is None  # Only 1 sample, needs 5

    def test_get_best_tool_sufficient_data(self, tracker):
        for i in range(6):
            tracker.record('feature', 'kilocode', 10.0, True)
        for i in range(6):
            tracker.record('feature', 'opencode', 15.0, False)
        result = tracker.get_best_tool('feature')
        assert result is not None
        tool_name, success_rate, avg_time = result
        assert tool_name == 'kilocode'
        assert success_rate == 1.0

    def test_export_learning_delta(self, tracker):
        for i in range(6):
            tracker.record('code_review', 'claude_code', 4.0, True)
        delta = tracker.export_learning_delta()
        assert delta is not None
        assert 'coding_benchmarks' in delta
        assert 'code_review' in delta['coding_benchmarks']
        assert 'claude_code' in delta['coding_benchmarks']['code_review']

    def test_export_empty(self, tracker):
        delta = tracker.export_learning_delta()
        assert delta is None

    def test_import_hive_delta(self, tracker):
        hive_data = {
            'coding_benchmarks': {
                'bug_fix': {
                    'claude_code': {'success_rate': 0.95, 'avg_time_s': 3.5, 'sample_count': 100},
                    'opencode': {'success_rate': 0.85, 'avg_time_s': 5.0, 'sample_count': 50},
                }
            }
        }
        tracker.import_hive_delta(hive_data)
        best = tracker.get_hive_best_tool('bug_fix')
        # get_hive_best_tool returns (tool_name, success_rate, avg_time_s) tuple
        assert best is not None
        assert best[0] == 'claude_code'
        assert best[1] == 0.95
        assert best[2] == 3.5


# ─── Tool Router Tests ───

class TestToolRouter:
    @patch('integrations.coding_agent.tool_router.get_available_backends')
    def test_user_override(self, mock_backends):
        mock_kilo = MagicMock()
        mock_kilo.name = 'kilocode'
        mock_backends.return_value = {'kilocode': mock_kilo}

        from integrations.coding_agent.tool_router import CodingToolRouter
        router = CodingToolRouter()
        result = router.route('test task', 'feature', user_override='kilocode')
        assert result == mock_kilo

    @patch('integrations.coding_agent.tool_router.get_available_backends')
    def test_no_tools_installed(self, mock_backends):
        mock_backends.return_value = {}
        from integrations.coding_agent.tool_router import CodingToolRouter
        router = CodingToolRouter()
        result = router.route('test task')
        assert result is None

    @patch('integrations.coding_agent.tool_router.get_available_backends')
    def test_heuristic_fallback(self, mock_backends):
        mock_claude = MagicMock()
        mock_claude.name = 'claude_code'
        mock_backends.return_value = {'claude_code': mock_claude}

        from integrations.coding_agent.tool_router import CodingToolRouter
        router = CodingToolRouter()
        result = router.route('review this', 'code_review')
        assert result == mock_claude


# ─── Feature Tier Map Tests ───

class TestFeatureTierMap:
    def test_new_feature_entries_exist(self):
        from security.system_requirements import FEATURE_TIER_MAP, NodeTierLevel
        assert 'coding_aggregator' in FEATURE_TIER_MAP
        assert 'vlm_computer_use' not in FEATURE_TIER_MAP   # no reader; removed
        assert 'whisper' not in FEATURE_TIER_MAP
        assert 'crawl4ai' in FEATURE_TIER_MAP
        assert 'minicpm_vision' in FEATURE_TIER_MAP
        assert 'video_captioning' in FEATURE_TIER_MAP
        assert 'vlm_omniparser' in FEATURE_TIER_MAP

    def test_tier_assignments(self):
        from security.system_requirements import FEATURE_TIER_MAP, NodeTierLevel
        assert FEATURE_TIER_MAP['coding_aggregator'][0] == NodeTierLevel.STANDARD
        assert FEATURE_TIER_MAP['crawl4ai'][0] == NodeTierLevel.LITE
        assert FEATURE_TIER_MAP['minicpm_vision'][0] == NodeTierLevel.FULL
        assert FEATURE_TIER_MAP['vlm_omniparser'][0] == NodeTierLevel.FULL
