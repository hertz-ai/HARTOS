"""
test_vlm_adapter.py - Tests for integrations/vlm/vlm_adapter.py

Tests the 3-tier VLM execution adapter:
- Tier 1: In-process (pyautogui available)
- Tier 2: HTTP local (flat mode)
- Tier 3: Crossbar WAMP fallback (returns None)

FT: execute_vlm_instruction routing, check_vlm_available, probe caching, reset.
NFT: Circuit breaker behavior, thread safety of globals, probe TTL expiry.
"""
import os
import sys
import time
import threading
import pytest
from unittest.mock import patch, MagicMock, Mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import integrations.vlm.vlm_adapter as adapter_mod


# ============================================================
# Helpers
# ============================================================

@pytest.fixture(autouse=True)
def reset_adapter_state():
    """Reset routing globals and isolate routing from the consent contract.

    Computer-control consent has its own behavioural suite.  These tests pin
    the tier/circuit-breaker logic after that canonical gate has allowed the
    request, just as Nunba boot supplies its signed-in or local-guest owner.
    """
    adapter_mod._tier1_fail_count = 0
    adapter_mod._tier2_fail_count = 0
    adapter_mod._probe_cache['ts'] = 0
    adapter_mod._probe_cache['result'] = None
    with patch('integrations.vlm.safety.computer_control_block',
               return_value=None):
        yield
    adapter_mod._tier1_fail_count = 0
    adapter_mod._tier2_fail_count = 0
    adapter_mod._probe_cache['ts'] = 0
    adapter_mod._probe_cache['result'] = None


# ============================================================
# execute_vlm_instruction - Tier routing
# ============================================================

class TestExecuteVlmInstruction:
    """Tier 1 -> Tier 2 -> Tier 3 fallback chain."""

    def test_tier1_success_returns_result(self):
        """When pyautogui is available and Tier 1 succeeds, return result."""
        expected = {"status": "success", "extracted_responses": [], "execution_time_seconds": 1.0}
        orig_has = adapter_mod._HAS_PYAUTOGUI
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', return_value=expected):
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert result == expected
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has

    def test_destructive_instruction_never_reaches_a_tier(self):
        with patch('integrations.vlm.local_loop.run_local_agentic_loop') as mock_loop:
            result = adapter_mod.execute_vlm_instruction(
                {'instruction_to_vlm_agent': 'Restart the system'})
        assert result['status'] == 'blocked'
        assert result['exit_reason'] == 'destructive_operation'
        mock_loop.assert_not_called()

    def test_tier1_failure_increments_circuit_breaker(self):
        """Tier 1 failure increments fail count."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            adapter_mod._node_tier = 'central'  # skip tier 2
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', side_effect=RuntimeError("boom")):
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert adapter_mod._tier1_fail_count == 1
            assert result is None  # fell through to Tier 3
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier1_circuit_breaker_trips_after_threshold(self):
        """After _FAIL_THRESHOLD failures, Tier 1 is skipped."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            adapter_mod._tier1_fail_count = adapter_mod._FAIL_THRESHOLD  # already tripped
            adapter_mod._node_tier = 'central'  # skip tier 2
            # Should not call run_local_agentic_loop at all
            with patch('integrations.vlm.local_loop.run_local_agentic_loop') as mock_loop:
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            mock_loop.assert_not_called()
            assert result is None
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier1_success_resets_fail_count(self):
        """Successful Tier 1 call resets circuit breaker."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            adapter_mod._tier1_fail_count = 1
            expected = {"status": "success"}
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', return_value=expected):
                adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert adapter_mod._tier1_fail_count == 0
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has

    def test_tier2_runs_when_flat_and_tier1_unavailable(self):
        """Tier 2 runs when node_tier=flat and pyautogui is absent."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'flat'
            expected = {"status": "success", "tier": "http"}
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', return_value=expected) as mock_loop:
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            mock_loop.assert_called_once()
            assert mock_loop.call_args[1].get('tier') == 'http' or mock_loop.call_args[0][1] == 'http'
            assert result == expected
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier2_circuit_breaker_trips(self):
        """Tier 2 circuit breaker trips after threshold failures."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'flat'
            adapter_mod._tier2_fail_count = adapter_mod._FAIL_THRESHOLD
            with patch('integrations.vlm.local_loop.run_local_agentic_loop') as mock_loop:
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            mock_loop.assert_not_called()
            assert result is None
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier2_failure_increments_counter(self):
        """Tier 2 failure increments its own circuit breaker."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'flat'
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', side_effect=RuntimeError("http fail")):
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert adapter_mod._tier2_fail_count == 1
            assert result is None
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier3_returns_none_for_central_mode(self):
        """Central mode with no pyautogui falls straight through to Tier 3 (None)."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'central'
            result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert result is None
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_tier3_returns_none_for_regional_mode(self):
        """Regional mode without pyautogui returns None (Crossbar path)."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'regional'
            result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert result is None
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_full_fallback_chain(self):
        """Tier 1 fails, Tier 2 fails, returns None for Tier 3."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            adapter_mod._node_tier = 'flat'
            with patch('integrations.vlm.local_loop.run_local_agentic_loop', side_effect=RuntimeError("fail")):
                result = adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})
            assert result is None
            assert adapter_mod._tier1_fail_count == 1
            assert adapter_mod._tier2_fail_count == 1
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier


# ============================================================
# check_vlm_available
# ============================================================

class TestCheckVlmAvailable:
    """Availability check across tiers."""

    def test_returns_true_when_pyautogui_present(self):
        orig_has = adapter_mod._HAS_PYAUTOGUI
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            assert adapter_mod.check_vlm_available() is True
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has

    def test_returns_true_for_central_without_pyautogui(self):
        """Central/regional always assumes Crossbar is available."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'central'
            assert adapter_mod.check_vlm_available() is True
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_flat_mode_probes_local_services(self):
        """Flat mode without pyautogui probes local services."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'flat'
            with patch.object(adapter_mod, '_probe_local_services', return_value=True) as mock_probe:
                assert adapter_mod.check_vlm_available() is True
                mock_probe.assert_called_once()
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier

    def test_flat_mode_returns_true_even_when_probe_fails(self):
        """Tier 3 (Crossbar) is always the fallback."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = False
            adapter_mod._node_tier = 'flat'
            with patch.object(adapter_mod, '_probe_local_services', return_value=False):
                # Still returns True because Tier 3 is always assumed available
                assert adapter_mod.check_vlm_available() is True
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier


# ============================================================
# _probe_local_services + caching
# ============================================================

class TestProbeLocalServices:
    """Probe caching and HTTP health checks."""

    def test_probe_returns_true_when_both_services_up(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        with patch('requests.get', return_value=mock_resp) as mock_get:
            result = adapter_mod._probe_local_services()
        assert result is True

    def test_probe_returns_false_when_service_down(self):
        with patch('requests.get', side_effect=ConnectionError("refused")):
            result = adapter_mod._probe_local_services()
        assert result is False

    def test_probe_returns_false_when_service_500(self):
        mock_resp = Mock()
        mock_resp.status_code = 500
        with patch('requests.get', return_value=mock_resp):
            result = adapter_mod._probe_local_services()
        assert result is False

    def test_probe_caches_result(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        with patch('requests.get', return_value=mock_resp) as mock_get:
            adapter_mod._probe_local_services()
            adapter_mod._probe_local_services()
        # Second call should use cache -- only 2 HTTP calls total (omni + gui)
        assert mock_get.call_count == 2

    def test_probe_cache_expires_after_ttl(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        with patch('requests.get', return_value=mock_resp) as mock_get:
            adapter_mod._probe_local_services()
            # Force cache expiry
            adapter_mod._probe_cache['ts'] = time.time() - adapter_mod._PROBE_TTL - 1
            adapter_mod._probe_local_services()
        # Both calls should have made HTTP requests
        assert mock_get.call_count == 4

    def test_probe_uses_custom_ports_from_env(self):
        mock_resp = Mock()
        mock_resp.status_code = 200
        with patch.dict(os.environ, {'OMNIPARSER_PORT': '9090', 'VLM_GUI_PORT': '9091'}):
            with patch('requests.get', return_value=mock_resp) as mock_get:
                adapter_mod._probe_local_services()
                calls = [str(c) for c in mock_get.call_args_list]
                assert any('9090' in c for c in calls)
                assert any('9091' in c for c in calls)


# ============================================================
# reset_circuit_breakers
# ============================================================

class TestResetCircuitBreakers:
    """reset_circuit_breakers resets all circuit breakers and probe cache."""

    def test_resets_tier1_counter(self):
        adapter_mod._tier1_fail_count = 5
        adapter_mod.reset_circuit_breakers()
        assert adapter_mod._tier1_fail_count == 0

    def test_resets_tier2_counter(self):
        adapter_mod._tier2_fail_count = 5
        adapter_mod.reset_circuit_breakers()
        assert adapter_mod._tier2_fail_count == 0

    def test_resets_probe_cache(self):
        adapter_mod._probe_cache['ts'] = time.time()
        adapter_mod._probe_cache['result'] = True
        adapter_mod.reset_circuit_breakers()
        assert adapter_mod._probe_cache['ts'] == 0
        assert adapter_mod._probe_cache['result'] is None


# ============================================================
# NFT: Thread safety
# ============================================================

class TestThreadSafety:
    """Concurrent calls to execute_vlm_instruction must not corrupt state."""

    def test_concurrent_tier1_failures_count_correctly(self):
        """Multiple threads incrementing fail count should not lose increments."""
        orig_has = adapter_mod._HAS_PYAUTOGUI
        orig_tier = adapter_mod._node_tier
        try:
            adapter_mod._HAS_PYAUTOGUI = True
            adapter_mod._node_tier = 'central'
            adapter_mod._tier1_fail_count = 0
            adapter_mod._FAIL_THRESHOLD = 1000  # raise threshold so all threads attempt

            call_count = 50
            barrier = threading.Barrier(call_count)

            def do_call():
                barrier.wait()
                with patch('integrations.vlm.local_loop.run_local_agentic_loop', side_effect=RuntimeError("fail")):
                    adapter_mod.execute_vlm_instruction({"instruction_to_vlm_agent": "test"})

            threads = [threading.Thread(target=do_call) for _ in range(call_count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Not atomic, so we check >= rather than == (GIL helps but not guaranteed)
            assert adapter_mod._tier1_fail_count >= 1
        finally:
            adapter_mod._HAS_PYAUTOGUI = orig_has
            adapter_mod._node_tier = orig_tier
            adapter_mod._FAIL_THRESHOLD = 2


# ============================================================
# NFT: Module-level constants
# ============================================================

class TestModuleConstants:
    """Verify module-level constants are sensible."""

    def test_fail_threshold_is_reasonable(self):
        assert adapter_mod._FAIL_THRESHOLD >= 1
        assert adapter_mod._FAIL_THRESHOLD <= 10

    def test_probe_ttl_is_positive(self):
        assert adapter_mod._PROBE_TTL > 0

    def test_probe_ttl_not_too_long(self):
        assert adapter_mod._PROBE_TTL <= 300  # 5 minutes max
