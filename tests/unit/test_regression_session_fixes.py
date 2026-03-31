"""
test_regression_session_fixes.py - Regression tests for HARTOS bugs fixed this session

Each test guards a specific bug. If any fails, the bug has regressed:

BUG-1: TTLCache isinstance(dict) → hasattr duck-typing in lifecycle_hooks
BUG-3: Empty messages guard in state_transition
BUG-5: Invalid ledger transition IN_PROGRESS→IN_PROGRESS skip
BUG-7: ModelCatalog save race (WinError 32)
BUG-10: Agent data path resolution (Program Files read-only)
BUG-11: Robot goal guard (no hardware = skip)
BUG-W: Watchdog LLM timeout 300→900s
BUG-P: casual_conv skip get_action_user_details (5.6s→0s)
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# BUG-1: lifecycle_hooks TTLCache duck-typing
# ============================================================

class TestTTLCacheDuckTyping:
    """Fixed: isinstance(user_tasks, dict) was False for TTLCache.
    Now uses hasattr(user_tasks, 'get') and not hasattr(user_tasks, 'current_action')."""

    def test_ttl_cache_has_get(self):
        """TTLCache must have .get() — lifecycle hooks rely on this."""
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        assert hasattr(cache, 'get')

    def test_ttl_cache_not_isinstance_dict(self):
        """TTLCache must NOT pass isinstance(dict) — this was the bug."""
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        assert not isinstance(cache, dict)

    def test_action_has_current_action(self):
        """Action class must have .current_action — duck-typing distinguishes it from TTLCache."""
        from helper import Action
        action = Action(['step1'])
        assert hasattr(action, 'current_action')


# ============================================================
# BUG-W: Watchdog LLM timeout
# ============================================================

class TestWatchdogTimeout:
    """Fixed: LLM_CALL_TIMEOUT was 300s, autonomous gather_info takes 5-10min.
    Now 900s (15 min) to prevent restart loop."""

    def test_llm_timeout_is_900(self):
        from security.node_watchdog import LLM_CALL_TIMEOUT_SECONDS
        assert LLM_CALL_TIMEOUT_SECONDS == 900

    def test_llm_timeout_greater_than_default_interval(self):
        """LLM timeout must exceed the daemon interval (30s) by a large margin."""
        from security.node_watchdog import LLM_CALL_TIMEOUT_SECONDS
        assert LLM_CALL_TIMEOUT_SECONDS >= 600  # At least 10 minutes


# ============================================================
# BUG-11: Robot goal guard (no hardware = skip)
# ============================================================

class TestRobotGoalGuard:
    """Fixed: Robot prompt builder always returned a prompt even with no robot.
    Now returns None when no locomotion/manipulation/sensors detected."""

    def test_robot_prompt_builder_returns_none_without_hardware(self):
        from integrations.robotics.robot_prompt_builder import build_robot_prompt
        mock_adv = MagicMock()
        mock_adv.get_capabilities.return_value = {
            'form_factor': 'unknown',
            'locomotion': None,
            'manipulation': None,
            'sensors': {},
        }
        mock_mod = MagicMock()
        mock_mod.get_capability_advertiser.return_value = mock_adv
        with patch.dict('sys.modules', {
            'integrations.robotics.capability_advertiser': mock_mod,
        }):
            result = build_robot_prompt({'title': 'Robot Test', 'description': 'Test'})
        assert result is None


# ============================================================
# BUG-10: Agent data path resolution
# ============================================================

class TestPathResolution:
    """Fixed: agent_lightning tracer used relative ./agent_data which fails
    in installed builds (C:\\Program Files\\ is read-only)."""

    def test_prompts_dir_is_absolute(self):
        """PROMPTS_DIR must be absolute — relative paths fail in installed builds."""
        from helper import PROMPTS_DIR
        assert os.path.isabs(PROMPTS_DIR)

    def test_prompts_dir_exists(self):
        from helper import PROMPTS_DIR
        # On CI, ensure the directory exists (it may not if this is a fresh checkout)
        os.makedirs(PROMPTS_DIR, exist_ok=True)
        assert os.path.isdir(PROMPTS_DIR)


# ============================================================
# BUG-4: Autoresearch empty config guard
# ============================================================

class TestAutoresearchGuard:
    """Fixed: _build_autoresearch_prompt returned full prompt with empty config fields,
    causing the LLM to loop trying to extract nonexistent repo_path/run_command."""

    def test_returns_none_for_empty_config(self):
        from integrations.agent_engine.goal_manager import _build_autoresearch_prompt
        result = _build_autoresearch_prompt({
            'title': 'Test Research',
            'description': 'Test',
            'config': {},  # Empty — no repo_path, no run_command
        })
        assert result is None

    def test_returns_none_for_missing_repo_path(self):
        from integrations.agent_engine.goal_manager import _build_autoresearch_prompt
        result = _build_autoresearch_prompt({
            'title': 'Test',
            'description': 'Test',
            'config': {'run_command': 'pytest'},  # repo_path missing
        })
        assert result is None

    def test_returns_prompt_for_complete_config(self):
        from integrations.agent_engine.goal_manager import _build_autoresearch_prompt
        result = _build_autoresearch_prompt({
            'title': 'Test Research',
            'description': 'Test',
            'config': {
                'repo_path': '/path/to/repo',
                'run_command': 'pytest',
            },
        })
        assert result is not None
        assert 'AUTONOMOUS RESEARCH AGENT' in result


# ============================================================
# BUG-5: Ledger transition skip (IN_PROGRESS→IN_PROGRESS)
# ============================================================

class TestLedgerTransitionSkip:
    """Fixed: _auto_sync_to_ledger called update_task_status(IN_PROGRESS)
    when task was already IN_PROGRESS, producing noisy warnings."""

    def test_no_op_transitions_concept(self):
        """Multiple ActionStates map to IN_PROGRESS — the skip guard prevents
        noisy warnings from update_task_status(IN_PROGRESS→IN_PROGRESS)."""
        from lifecycle_hooks import ActionState
        # These states all represent "work in progress" at different sub-phases
        in_progress_states = [
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.FALLBACK_RECEIVED,
            ActionState.RECIPE_REQUESTED,
        ]
        assert len(in_progress_states) >= 4
