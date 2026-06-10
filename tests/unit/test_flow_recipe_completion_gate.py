"""_flow_recipe_exists (agent_daemon) — CREATE/REUSE classifier signal.

Recipe existence routes dispatch (CREATE when absent, REUSE when present).
It is deliberately NOT a completion signal: a recipe proves a procedure ran
once, not that the goal's economic outcome happened — completion is measured
in spark transacted (steward decision 2026-06-10). The completion gate stays
spark-based; the local-work metering gap is tracked separately.

Behavioural: imports the real helper, points the canonical prompts-dir
resolver at a tmp dir, asserts observable decisions.
"""
import os
import sys
from unittest.mock import patch

import pytest  # noqa: F401  (parametrize-ready; keeps harness conventions)

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
