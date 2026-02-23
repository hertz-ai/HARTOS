"""
Tests for Coding Agent Orchestrator — compute-aware routing, hive offload, benchmarking.

Tests the orchestrator as a leaf tool (never re-dispatches to /chat).
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock


class TestOrchestratorInit:
    def test_singleton(self):
        from integrations.coding_agent.orchestrator import get_coding_orchestrator
        o1 = get_coding_orchestrator()
        o2 = get_coding_orchestrator()
        assert o1 is o2

    def test_list_tools(self):
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        with patch('integrations.coding_agent.installer.get_tool_info', return_value={
            'kilocode': {'installed': False}, 'claude_code': {'installed': False},
            'opencode': {'installed': False},
        }):
            result = orch.list_tools()
            assert 'tools' in result
            assert 'benchmarks' in result
            assert 'can_run_locally' in result


class TestComputeAwareRouting:
    def test_can_run_locally_when_feature_not_in_map(self):
        """If the feature is not in FEATURE_TIER_MAP, allow by default."""
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        # Patch FEATURE_TIER_MAP to not have the feature
        with patch('security.system_requirements.FEATURE_TIER_MAP', {}):
            assert orch._can_run_locally() is True

    @patch('security.system_requirements.get_tier')
    @patch('security.system_requirements._TIER_RANK',
           {'embedded': 0, 'observer': 1, 'lite': 2, 'standard': 3, 'full': 4, 'compute_host': 5})
    def test_can_run_locally_standard_tier(self, mock_tier):
        from security.system_requirements import NodeTierLevel
        mock_tier.return_value = NodeTierLevel.STANDARD
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        assert orch._can_run_locally() is True


class TestExecuteLocal:
    @patch('integrations.coding_agent.tool_router.CodingToolRouter.route')
    @patch('integrations.coding_agent.benchmark_tracker.get_benchmark_tracker')
    def test_execute_no_tools(self, mock_tracker, mock_route):
        mock_route.return_value = None
        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        result = orch._execute_local('test', 'feature', '', '', '', '')
        assert result['success'] is False
        assert 'No coding tools installed' in result['error']

    @patch('integrations.coding_agent.tool_router.CodingToolRouter.route')
    @patch('integrations.coding_agent.benchmark_tracker.get_benchmark_tracker')
    def test_execute_with_backend(self, mock_tracker_fn, mock_route):
        mock_backend = MagicMock()
        mock_backend.name = 'kilocode'
        mock_backend.execute.return_value = {
            'success': True, 'output': 'done', 'tool': 'kilocode',
            'execution_time_s': 2.5,
        }
        mock_route.return_value = mock_backend

        mock_tracker = MagicMock()
        mock_tracker_fn.return_value = mock_tracker

        from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
        orch = CodingAgentOrchestrator()
        result = orch._execute_local('implement login', 'feature', '', 'user1', '', '')

        assert result['success'] is True
        assert result['tool'] == 'kilocode'
        assert result['task_type'] == 'feature'
        mock_tracker.record.assert_called_once()


class TestAntipatterns:
    """Verify the orchestrator is a LEAF tool — no /chat re-entry."""

    def test_execute_does_not_import_chat(self):
        """Ensure orchestrator.execute() never imports langchain_gpt_api or dispatch_to_chat."""
        import integrations.coding_agent.orchestrator as orch_module
        source = open(orch_module.__file__, 'r').read()
        assert 'dispatch_to_chat' not in source
        assert 'langchain_gpt_api' not in source
        assert "'/chat'" not in source

    def test_tool_backends_no_chat_dispatch(self):
        """Ensure tool_backends.py never dispatches to /chat."""
        import integrations.coding_agent.tool_backends as tb_module
        source = open(tb_module.__file__, 'r').read()
        assert 'dispatch_to_chat' not in source
        assert "'/chat'" not in source

    def test_tool_router_no_chat_dispatch(self):
        """Ensure tool_router.py never dispatches to /chat."""
        import integrations.coding_agent.tool_router as tr_module
        source = open(tr_module.__file__, 'r').read()
        assert 'dispatch_to_chat' not in source
        assert "'/chat'" not in source


class TestVLMAdapterFix:
    """Verify VLM adapter no longer requires BUNDLED_MODE."""

    def test_vlm_adapter_no_bundled_gate(self):
        import integrations.vlm.vlm_adapter as vlm_module
        source = open(vlm_module.__file__, 'r').read()
        # The condition should NOT have _BUNDLED_MODE as a required gate
        # Old: if _BUNDLED_MODE and _HAS_PYAUTOGUI and ...
        # New: if _HAS_PYAUTOGUI and ...
        lines = source.split('\n')
        for line in lines:
            if '_HAS_PYAUTOGUI' in line and 'if ' in line and '_tier1_fail_count' in line:
                assert '_BUNDLED_MODE' not in line, \
                    f"VLM Tier 1 still gated behind _BUNDLED_MODE: {line.strip()}"

    def test_vlm_check_available_no_bundled_gate(self):
        import integrations.vlm.vlm_adapter as vlm_module
        source = open(vlm_module.__file__, 'r').read()
        # check_vlm_available should not require _BUNDLED_MODE
        in_check_fn = False
        for line in source.split('\n'):
            if 'def check_vlm_available' in line:
                in_check_fn = True
            elif in_check_fn and line.strip().startswith('def '):
                break
            elif in_check_fn and '_HAS_PYAUTOGUI' in line:
                assert '_BUNDLED_MODE' not in line, \
                    f"check_vlm_available still gated behind _BUNDLED_MODE"


class TestOmniParserFallback:
    """Verify OmniParser gracefully falls back when HTTP parse fails."""

    def test_http_parse_fallback(self):
        from integrations.vlm.local_omniparser import _parse_http
        import requests as _req
        with patch.object(_req, 'post', side_effect=_req.RequestException("Connection refused")):
            result = _parse_http('dummybase64data')
            assert result['screen_info'] == ''
            assert result['parsed_content_list'] == []
            assert 'latency' in result
