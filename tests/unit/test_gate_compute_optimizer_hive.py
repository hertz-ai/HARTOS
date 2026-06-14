"""Behavioural gate test for ComputeOptimizer._hive_explore_loop.

Verifies the user-activity yield gate added to the hive-exploration loop:

  * when ``core.foreground.should_yield_to_user`` (imported into
    ``core.compute_optimizer``) reports True, the loop SKIPS the heavy
    ``_explore_hive_stream()`` work for that tick and defers via the loop's
    OWN ``_stop_event.wait`` sleep primitive (NO busy-spin, NO bare continue);
  * when it reports False, the heavy ``_explore_hive_stream()`` IS attempted.

This is NOT a grep/source-shape test: it constructs a real ComputeOptimizer,
mocks every I/O boundary (the heavy hive call, the long sleep, randomness),
drives exactly one loop iteration, and asserts on the observable side-effect
(whether the heavy work's mock was called).

Run:
  venv/Scripts/python.exe -c "import os; os._walk_symlinks_as_files=False; \
    import sys,pytest; sys.exit(pytest.main(sys.argv[1:]))" \
    tests/unit/test_gate_compute_optimizer_hive.py -p no:cacheprovider \
    --noconftest -q
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.compute_optimizer import ComputeOptimizer, HIVE_EXPLORE_MIN


class TestHiveExploreLoopYieldGate(unittest.TestCase):
    """One-iteration drive of _hive_explore_loop under both gate verdicts."""

    def _run_one_tick(self, yield_verdict: bool):
        """Drive _hive_explore_loop for exactly one iteration.

        Returns the optimizer so the caller can assert on its mocks.

        Boundaries mocked so NO real I/O / sleep / randomness happens:
          * ``should_yield_to_user`` (THIS module's import) -> ``yield_verdict``
          * ``_explore_hive_stream``  -> a no-op mock (the heavy work under test)
          * ``self._stop_event.wait`` -> a fake that never really sleeps and
            forces the loop to exit after a single body execution.
          * ``random.uniform``        -> a fixed delay (deterministic)
        """
        opt = ComputeOptimizer()

        # The heavy work we are gating — replace with a recording no-op so the
        # real network/goal-manager path never runs.
        opt._explore_hive_stream = unittest_mock_call_recorder()

        # Drive the loop to run its body once then stop.  ``wait`` is the loop's
        # OWN sleep primitive; we make it record its calls (so we can prove the
        # defer slept rather than busy-spun) and steer loop termination:
        #
        #   yield=True  path:  enters loop -> gate True -> wait(RECHECK) -> break
        #     We return True on the FIRST wait so the deferral's
        #     ``if self._stop_event.wait(...): break`` exits immediately AND no
        #     heavy work runs.  (A bare ``continue`` here would loop forever in
        #     this test — its absence is exactly what we want to enforce.)
        #
        #   yield=False path:  enters loop -> gate False -> wait(delay) False ->
        #     body runs _explore_hive_stream -> back to while -> stop_event now
        #     set -> loop exits.
        wait_calls = []

        def fake_wait(timeout=None):
            wait_calls.append(timeout)
            if yield_verdict:
                # Deferral sleep: returning True triggers the ``break`` so the
                # test ends after a single deferred (heavy-work-free) tick.
                return True
            # Non-yield path: first (and only) sleep before the body returns
            # False so the body executes; arm the stop so the NEXT while-check
            # ends the loop after one real iteration.
            opt._stop_event.set()
            return False

        with patch('core.compute_optimizer.should_yield_to_user',
                   return_value=yield_verdict), \
             patch.object(opt._stop_event, 'wait', side_effect=fake_wait), \
             patch('core.compute_optimizer.random.uniform', return_value=123.0):
            opt._hive_explore_loop()

        # Defer/sleep must have actually been invoked (loop's own primitive) —
        # this is the busy-spin guard: a bare ``continue`` would never call wait.
        self.assertTrue(wait_calls, "loop must sleep via _stop_event.wait, not busy-spin")
        return opt

    def test_skips_heavy_work_when_user_active(self):
        """Gate True -> _explore_hive_stream is NOT called this tick."""
        opt = self._run_one_tick(yield_verdict=True)
        self.assertEqual(
            opt._explore_hive_stream.call_count, 0,
            "heavy hive exploration must be SKIPPED while the user is active / box hot",
        )

    def test_runs_heavy_work_when_idle(self):
        """Gate False -> _explore_hive_stream IS attempted."""
        opt = self._run_one_tick(yield_verdict=False)
        self.assertGreaterEqual(
            opt._explore_hive_stream.call_count, 1,
            "heavy hive exploration must run when the user is idle and the box is cool",
        )

    def test_recheck_uses_loop_sleep_primitive_cadence(self):
        """The defer sleeps for HIVE_EXPLORE_MIN via the loop's own wait()."""
        opt = ComputeOptimizer()
        opt._explore_hive_stream = unittest_mock_call_recorder()

        seen = []

        def fake_wait(timeout=None):
            seen.append(timeout)
            return True  # break out after recording the deferral timeout

        with patch('core.compute_optimizer.should_yield_to_user', return_value=True), \
             patch.object(opt._stop_event, 'wait', side_effect=fake_wait):
            opt._hive_explore_loop()

        self.assertEqual(
            seen[0], HIVE_EXPLORE_MIN,
            "deferral must reuse the loop's own cadence floor (HIVE_EXPLORE_MIN)",
        )
        self.assertEqual(opt._explore_hive_stream.call_count, 0)


def unittest_mock_call_recorder():
    """A tiny call-recording callable (MagicMock would also work; kept explicit
    so the side-effect-free no-op intent is obvious)."""
    from unittest.mock import MagicMock
    return MagicMock(return_value=None)


if __name__ == '__main__':
    unittest.main()
