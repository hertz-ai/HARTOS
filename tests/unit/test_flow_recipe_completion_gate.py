"""Completion-gate evidence: _flow_recipe_exists (agent_daemon).

The flywheel deadlock this guards against (live-witnessed 2026-06-10):
local llama work costs 0 Spark BY DESIGN (budget_gate), so on an all-local
box `goal.spark_spent` never rises — the old spark-only completion gate made
goal completion structurally impossible (every goal noop'd 5x then
auto-paused; `completed` stuck at 13 while 45 goals sat paused). The flow
recipe artifact — written only by after_all_actions_terminated() — is the
durable real-work signal that holds on a free-local box.

Behavioural: imports the real helper, points the canonical prompts-dir
resolver at a tmp dir, asserts observable decisions.
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.agent_daemon import _flow_recipe_exists  # noqa: E402


class TestFlowRecipeExists:
    def test_true_when_flow0_recipe_present(self, tmp_path):
        pid = 4242
        (tmp_path / f"{pid}_0_recipe.json").write_text("[]", encoding="utf-8")
        with patch("core.platform_paths.get_recipe_prompts_dir",
                   return_value=str(tmp_path)):
            assert _flow_recipe_exists(pid) is True

    def test_false_when_absent(self, tmp_path):
        with patch("core.platform_paths.get_recipe_prompts_dir",
                   return_value=str(tmp_path)):
            assert _flow_recipe_exists(99999) is False

    def test_false_for_other_goals_recipe(self, tmp_path):
        """Hash-keyed filename: another goal's recipe must not count."""
        (tmp_path / "1111_0_recipe.json").write_text("[]", encoding="utf-8")
        with patch("core.platform_paths.get_recipe_prompts_dir",
                   return_value=str(tmp_path)):
            assert _flow_recipe_exists(2222) is False

    def test_resolver_failure_degrades_to_false_not_raise(self):
        """A broken prompts-dir resolver must never crash the daemon tick."""
        with patch("core.platform_paths.get_recipe_prompts_dir",
                   side_effect=RuntimeError("boom")):
            # falls back to 'prompts' relative dir; nonexistent id -> False
            assert _flow_recipe_exists("no_such_goal_id_xyz") is False


class TestGateDecision:
    """The gate's decision table, exercised through the same helper the
    daemon uses: complete iff spark>0 OR recipe exists (non-continuous)."""

    @pytest.mark.parametrize(
        "spark,recipe_on_disk,should_complete",
        [
            (0, False, False),   # nothing happened -> noop path
            (0, True, True),     # free local work, recipe proof -> complete
            (5, False, True),    # metered cloud work -> complete
            (5, True, True),     # both signals -> complete
        ],
    )
    def test_decision(self, tmp_path, spark, recipe_on_disk, should_complete):
        pid = 7
        if recipe_on_disk:
            (tmp_path / f"{pid}_0_recipe.json").write_text("[]",
                                                           encoding="utf-8")
        with patch("core.platform_paths.get_recipe_prompts_dir",
                   return_value=str(tmp_path)):
            decision = spark > 0 or _flow_recipe_exists(pid)
        assert decision is should_complete
