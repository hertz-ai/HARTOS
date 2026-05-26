"""Regression tests for the bootstrap_provision_tts_venvs seed goal.

Pins the contract that the seed goal:
  1. Exists in SEED_BOOTSTRAP_GOALS with the expected slug.
  2. Names both the canonical existence check (is_venv_healthy) and the
     canonical install tool (repair_backend_venv) by exact identifier —
     the LLM uses the description as its execution playbook, so the
     tool names being verbatim is load-bearing.
  3. Explicitly tells the agent to run ONE install per dispatch (the
     entire pacing design depends on this — parallel installs would
     defeat the purpose).
  4. Uses goal_type='provision' (canonical category for "set up
     infrastructure" work, distinct from self_heal which is auto-
     created reactively by error_advice).
  5. Excludes piper / espeak / pocket_tts / mms_tts from the work
     (they're bundled or run in main; including them would crash
     repair_backend_venv with 'engine has install_target=main').
  6. Spark budget is sane (small enough to bound runaway, large enough
     to fit ~5 engines × ~30 Spark each).
  7. Slug is unique within SEED_BOOTSTRAP_GOALS (idempotent reseed).
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class ProvisionVenvsSeedContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        cls.seed = next(
            (g for g in SEED_BOOTSTRAP_GOALS
             if g.get('slug') == 'bootstrap_provision_tts_venvs'),
            None,
        )

    def test_seed_entry_exists(self):
        self.assertIsNotNone(
            self.seed,
            "bootstrap_provision_tts_venvs entry not found in "
            "SEED_BOOTSTRAP_GOALS — the seed is what makes the daemon "
            "actually run the venv provisioning loop on boot.",
        )

    def test_goal_type_is_provision(self):
        self.assertEqual(
            self.seed.get('goal_type'), 'provision',
            "goal_type must be 'provision' (set-up-infrastructure category) — "
            "not 'self_heal' (which is for reactive error_advice-spawned goals) "
            "or 'coding' (which routes to the coding daemon, not the main "
            "agent_daemon).",
        )

    def test_description_names_canonical_tools_verbatim(self):
        """The LLM uses the description as its playbook.  The tool
        identifiers must be exact — `repair_backend_venv` and
        `is_venv_healthy` — otherwise the agent generates near-misses
        like 'fix_venv' that don't exist as registered tools."""
        desc = self.seed.get('description', '')
        self.assertIn(
            'repair_backend_venv', desc,
            "description must name the canonical install tool "
            "(integrations/coding_agent/backend_repair_tools.py:"
            "repair_backend_venv) — the agent generates the tool call "
            "from this exact string.",
        )
        self.assertIn(
            'is_venv_healthy', desc,
            "description must name the canonical health check "
            "(tts/backend_venv.py:is_venv_healthy) so the agent "
            "filters by it instead of attempting to install every "
            "engine on every tick.",
        )

    def test_description_enforces_one_install_per_dispatch(self):
        """The entire pacing design depends on this — without it the
        agent would loop over every unhealthy engine in one dispatch
        and fire parallel pip installs, defeating the purpose."""
        desc = self.seed.get('description', '').lower()
        # Look for either explicit 'one' phrasing or the per-tick marker.
        self.assertTrue(
            'one install per dispatch' in desc
            or 'one per dispatch' in desc
            or ('exactly once' in desc and 'never parallelise' in desc),
            "description must explicitly require ONE install per "
            "dispatch — without this the LLM defaults to looping "
            "over every unhealthy engine, firing parallel pip "
            "installs, and spiking system load.  Found description: "
            f"{desc[:200]!r}…",
        )

    def test_description_excludes_main_interpreter_engines(self):
        """piper / espeak / pocket_tts / mms_tts must NOT be installed
        via repair_backend_venv — they're bundled or main-interpreter
        engines.  Calling repair_backend_venv on them would fail
        because their spec has install_target != 'venv'."""
        desc = self.seed.get('description', '').lower()
        for excluded in ('piper', 'espeak', 'pocket_tts', 'mms_tts'):
            self.assertIn(
                excluded, desc,
                f"description must explicitly exclude {excluded!r} "
                "from the install loop — repair_backend_venv would "
                "fail on it because its spec is install_target=main "
                "or install_target=bundled.",
            )

    def test_spark_budget_in_sane_range(self):
        """Budget must be small enough to bound runaway (<= 500) but
        large enough to fit ~5 engines × ~30 Spark per install."""
        budget = self.seed.get('spark_budget', 0)
        self.assertGreaterEqual(
            budget, 100,
            "spark_budget < 100 risks running out mid-install on a "
            "fresh deploy with 4-5 unhealthy engines.",
        )
        self.assertLessEqual(
            budget, 500,
            "spark_budget > 500 is runaway protection territory — "
            "the loop should complete in well under that even with "
            "every engine to install.",
        )

    def test_config_marks_continuous_monitor(self):
        cfg = self.seed.get('config', {})
        self.assertEqual(cfg.get('mode'), 'monitor',
                         "mode='monitor' so the daemon re-ticks rather than "
                         "marking the goal completed after the first dispatch.")
        self.assertTrue(cfg.get('continuous'),
                        "continuous=True so the goal keeps checking — "
                        "is_venv_healthy returns True for healthy engines, "
                        "making the warm-boot tick free.")

    def test_priority_yields_to_user_goals(self):
        """priority=2 keeps this BELOW user-facing goals (typically
        priority 0-1) so venv installs never preempt chat or
        marketing dispatches."""
        cfg = self.seed.get('config', {})
        priority = cfg.get('priority', 0)
        self.assertGreaterEqual(
            priority, 2,
            "priority must be >= 2 so venv installs don't preempt "
            "user-facing goals.  The pacing design depends on this "
            "as much as on the one-install-per-dispatch rule.",
        )

    def test_slug_unique_in_seed_list(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g.get('slug') for g in SEED_BOOTSTRAP_GOALS]
        self.assertEqual(
            slugs.count('bootstrap_provision_tts_venvs'), 1,
            "bootstrap_provision_tts_venvs slug appears multiple times "
            "in SEED_BOOTSTRAP_GOALS — duplicate seed would cause the "
            "idempotent reseed to leave only one row but log a noisy "
            "warning per boot.",
        )


class EveryVenvEligibleEngineIsImpliedByDescription(unittest.TestCase):
    """Cross-check: every engine_id with install_target='venv' in the
    actual ENGINE_REGISTRY is covered by the seed goal's instruction.
    The description doesn't name individual engines (it filters at
    runtime by install_target=='venv'), but the EXCLUSION list must
    NOT accidentally name a venv-eligible engine."""

    def test_excluded_engines_are_actually_main_or_bundled(self):
        """piper/espeak/pocket_tts/mms_tts are in the description's
        exclusion list — verify the ENGINE_REGISTRY agrees."""
        try:
            from integrations.channels.media.tts_router import ENGINE_REGISTRY
        except Exception as e:
            self.skipTest(f"ENGINE_REGISTRY not importable: {e}")

        excluded = ('piper', 'espeak', 'pocket_tts', 'mms_tts')
        for engine_id in excluded:
            spec = ENGINE_REGISTRY.get(engine_id)
            if spec is None:
                continue  # engine was removed — description harmlessly stale
            install_target = getattr(spec, 'install_target', 'main')
            self.assertNotEqual(
                install_target, 'venv',
                f"{engine_id} is in the seed goal's EXCLUSION list but "
                f"its spec declares install_target='venv'.  Either the "
                f"spec changed (need to remove from exclusion list) or "
                f"the exclusion list is wrong (need to remove from "
                f"description).",
            )


if __name__ == '__main__':
    unittest.main()
