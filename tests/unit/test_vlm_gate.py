"""Tests for the VLM benchmark's no-regression gate (Phase 0 of
memory/vlm_best_of_all_worlds_plan.md).

The gate IS the regression guard for every other phase.  If the gate
itself has a bug it doesn't catch real regressions, so this test
suite is non-negotiable.

Each test case is a pure dict-in / list-out check against the
helpers in tests/vlm_gate_lib.py — no benchmark execution, no VLM
server needed."""

import os
import sys
import unittest

# tests/vlm_gate_lib.py lives in tests/ — add to path so unit tests
# in tests/unit/ can import it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from vlm_gate_lib import (  # noqa: E402
    summarize_bucket, strategy_attribution,
    compare_buckets, compare_attribution, compare_router_decisions,
    render_baseline_md,
)


def _row(target, strategy=None, method=None, error=50.0, time_=1.0):
    """Construct a benchmark-result dict the helpers expect."""
    r = {'target': target, 'error': error, 'time': time_}
    if strategy is not None:
        r['strategy'] = strategy
    if method is not None:
        r['method'] = method
    return r


class TestSummarizeBucket(unittest.TestCase):
    """summarize_bucket: groups items, drops FAILs from avg_err but
    counts them in fail_count, treats time as total wall-clock cost."""

    def test_groups_by_key(self):
        items = [
            _row('Start',  strategy='describe_first', error=20),
            _row('Search', strategy='describe_first', error=40),
            _row('Start',  strategy='direct',         error=200),
        ]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(set(out.keys()), {'describe_first', 'direct'})
        self.assertEqual(out['describe_first']['n'], 2)
        self.assertEqual(out['describe_first']['avg_err'], 30.0)
        self.assertEqual(out['direct']['avg_err'], 200.0)

    def test_exact_count_threshold_30(self):
        """error < 30 = EXACT (per benchmark grading)."""
        items = [
            _row('A', strategy='s', error=29),  # EXACT
            _row('B', strategy='s', error=30),  # not EXACT (boundary)
            _row('C', strategy='s', error=200),
        ]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(out['s']['exact_count'], 1)

    def test_fail_excluded_from_avg_but_counted(self):
        """A 9999 FAIL must NOT swamp the avg_err — that's the whole
        reason for the FAIL/EXACT split.  But it must show up in
        fail_count so the regression check can flag it."""
        items = [
            _row('A', strategy='s', error=10),
            _row('B', strategy='s', error=20),
            _row('C', strategy='s', error=9999),  # FAIL
        ]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(out['s']['avg_err'], 15.0,
            "FAIL polluted avg_err — gate would miss real grounding drift")
        self.assertEqual(out['s']['fail_count'], 1)

    def test_all_fail_falls_back_to_raw_avg(self):
        """When every item is FAIL, clean=[] would div-by-zero.
        Helper falls back to using the unclean list."""
        items = [
            _row('A', strategy='s', error=9999),
            _row('B', strategy='s', error=9999),
        ]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(out['s']['avg_err'], 9999.0)
        self.assertEqual(out['s']['fail_count'], 2)

    def test_avg_time_uses_all_items(self):
        """FAILs still cost wall-clock — gate must catch a slowdown
        even if accuracy held."""
        items = [
            _row('A', strategy='s', error=10, time_=1.0),
            _row('B', strategy='s', error=9999, time_=5.0),
        ]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(out['s']['avg_time_s'], 3.0)

    def test_missing_key_skipped(self):
        items = [_row('A', error=10), _row('B', strategy='s', error=20)]
        out = summarize_bucket(items, 'strategy')
        self.assertEqual(set(out.keys()), {'s'})


class TestStrategyAttribution(unittest.TestCase):

    def test_records_per_target_method_strategy(self):
        items = [
            {'target': 'Start', 'method': 'point_and_act',
             'strategy': 'taskbar_pre_check', 'error': 5, 'time': 0.5},
            {'target': 'Start', 'method': 'loop_one_iter',
             'strategy': 'inline_prompt', 'error': 10, 'time': 1.0},
        ]
        attr = strategy_attribution(items)
        self.assertEqual(attr[('Start', 'point_and_act')], 'taskbar_pre_check')
        self.assertEqual(attr[('Start', 'loop_one_iter')], 'inline_prompt')

    def test_missing_strategy_defaults_to_qmark(self):
        items = [{'target': 'Start', 'method': 'm', 'error': 1, 'time': 0.1}]
        attr = strategy_attribution(items)
        self.assertEqual(attr[('Start', 'm')], '?')


class TestCompareBuckets(unittest.TestCase):
    """The four threshold checks: avg_err / EXACT / FAIL / avg_time.
    Each needs at least one PASS test + one FAIL test."""

    def _bucket(self, avg_err=50, exact=2, fail=0, avg_time=1.0, n=4):
        return {'b': {'avg_err': avg_err, 'exact_count': exact,
                      'fail_count': fail, 'avg_time_s': avg_time, 'n': n}}

    def test_no_regression_when_identical(self):
        baseline = self._bucket()
        current = self._bucket()
        self.assertEqual(compare_buckets(current, baseline, 'x'), [])

    def test_no_regression_when_better(self):
        baseline = self._bucket(avg_err=100, exact=1, fail=2, avg_time=5)
        current = self._bucket(avg_err=10, exact=4, fail=0, avg_time=1)
        self.assertEqual(compare_buckets(current, baseline, 'x'), [])

    def test_avg_err_above_threshold_fails(self):
        baseline = self._bucket(avg_err=50)
        # +20% with abs fuzz of +5 → tolerable up to 50*1.10+5 = 60
        current = self._bucket(avg_err=70)
        regressions = compare_buckets(current, baseline, 'x')
        self.assertEqual(len(regressions), 1)
        self.assertIn('avg_err 70', regressions[0])

    def test_avg_err_within_threshold_passes(self):
        """50 * 1.10 + 5 = 60 — still passes at 59."""
        baseline = self._bucket(avg_err=50)
        current = self._bucket(avg_err=59)
        self.assertEqual(compare_buckets(current, baseline, 'x'), [])

    def test_exact_decrease_fails(self):
        baseline = self._bucket(exact=3)
        current = self._bucket(exact=2)
        regressions = compare_buckets(current, baseline, 'x')
        self.assertEqual(len(regressions), 1)
        self.assertIn('EXACT 2', regressions[0])

    def test_fail_increase_fails(self):
        baseline = self._bucket(fail=0)
        current = self._bucket(fail=1)
        regressions = compare_buckets(current, baseline, 'x')
        self.assertEqual(len(regressions), 1)
        self.assertIn('FAIL 1', regressions[0])

    def test_avg_time_above_threshold_fails(self):
        baseline = self._bucket(avg_time=2.0)
        # 2.0 * 1.20 + 1.0 = 3.4 — 3.5 should fail
        current = self._bucket(avg_time=3.5)
        regressions = compare_buckets(current, baseline, 'x')
        self.assertEqual(len(regressions), 1)
        self.assertIn('avg_time 3.5s', regressions[0])

    def test_avg_time_within_threshold_passes(self):
        baseline = self._bucket(avg_time=2.0)
        current = self._bucket(avg_time=3.3)
        self.assertEqual(compare_buckets(current, baseline, 'x'), [])

    def test_missing_bucket_in_current_fails(self):
        """Bucket that existed in baseline but not in current = a
        method/strategy got removed.  That's a regression."""
        baseline = {'old': {'avg_err': 50, 'exact_count': 1,
                            'fail_count': 0, 'avg_time_s': 1, 'n': 1}}
        current: dict = {}
        regressions = compare_buckets(current, baseline, 'method')
        self.assertEqual(len(regressions), 1)
        self.assertIn('missing in current run', regressions[0])

    def test_threshold_overrides_propagate(self):
        """Stricter custom threshold catches what default would pass."""
        baseline = self._bucket(avg_err=50)
        current = self._bucket(avg_err=58)
        # Default 10% + 5 fuzz = up to 60 → 58 passes.
        self.assertEqual(compare_buckets(current, baseline, 'x'), [])
        # Tighten to 0% → 58 > 50 + 5 fuzz = 55 → fail.
        regressions = compare_buckets(current, baseline, 'x',
                                      err_threshold_pct=0.0)
        self.assertEqual(len(regressions), 1)


class TestCompareAttribution(unittest.TestCase):
    """Per §0 invariant: same target → same strategy chain unless a
    baseline-bump justifies the change."""

    def test_no_regression_when_strategies_match(self):
        baseline = {('Start', 'point_and_act'): 'taskbar_pre_check'}
        current = {('Start', 'point_and_act'): 'taskbar_pre_check'}
        self.assertEqual(compare_attribution(current, baseline), [])

    def test_silent_strategy_swap_fails(self):
        """Same target/method, different sub-strategy — even if
        accuracy looks the same, this is a code-path regression."""
        baseline = {('Start', 'point_and_act'): 'taskbar_pre_check'}
        current = {('Start', 'point_and_act'): 'describe_first'}
        regressions = compare_attribution(current, baseline)
        self.assertEqual(len(regressions), 1)
        self.assertIn('describe_first', regressions[0])
        self.assertIn('taskbar_pre_check', regressions[0])
        self.assertIn('silent drift', regressions[0])

    def test_missing_target_in_current_skipped(self):
        """Bucket compare already flags missing-target — attribution
        compare shouldn't double-report."""
        baseline = {('Start', 'point_and_act'): 'taskbar_pre_check'}
        current: dict = {}
        self.assertEqual(compare_attribution(current, baseline), [])


class TestCompareRouterDecisions(unittest.TestCase):
    """Phase 3.5: silent router drift = regression even if accuracy
    looks the same.  Verified by comparing per-task actual route
    against baseline."""

    def test_no_regression_when_routes_match(self):
        baseline = [{'task': 'click X', 'expected': 'single_shot',
                     'actual': 'single_shot', 'pass': True}]
        current = [{'task': 'click X', 'expected': 'single_shot',
                    'actual': 'single_shot', 'pass': True}]
        self.assertEqual(compare_router_decisions(current, baseline), [])

    def test_silent_route_drift_fails(self):
        """Same task, baseline says single_shot, current says enumerate
        — even if both pass their expected, the change in actual = drift."""
        baseline = [{'task': 'click X', 'expected': 'single_shot',
                     'actual': 'single_shot', 'pass': True}]
        current = [{'task': 'click X', 'expected': 'enumerate',
                    'actual': 'enumerate', 'pass': True}]
        regressions = compare_router_decisions(current, baseline)
        self.assertEqual(len(regressions), 1)
        self.assertIn('silent router drift', regressions[0])

    def test_missing_task_in_current_fails(self):
        """Task removed from router_results = regression."""
        baseline = [{'task': 'click X', 'expected': 'single_shot',
                     'actual': 'single_shot', 'pass': True}]
        current = []
        regressions = compare_router_decisions(current, baseline)
        self.assertEqual(len(regressions), 1)
        self.assertIn('missing in current', regressions[0])

    def test_new_task_failing_flagged(self):
        """A task added since baseline that FAILS should be flagged
        (forces author to bump baseline if intentional)."""
        baseline = []
        current = [{'task': 'new task', 'expected': 'single_shot',
                    'actual': 'enumerate', 'pass': False}]
        regressions = compare_router_decisions(current, baseline)
        self.assertEqual(len(regressions), 1)
        self.assertIn('new test', regressions[0])

    def test_new_task_passing_not_flagged(self):
        """A task added that PASSES is fine — author can baseline-bump
        whenever convenient."""
        baseline = []
        current = [{'task': 'new task', 'expected': 'single_shot',
                    'actual': 'single_shot', 'pass': True}]
        self.assertEqual(compare_router_decisions(current, baseline), [])


class TestRenderBaselineMd(unittest.TestCase):

    def test_renders_method_section(self):
        out = {
            'timestamp': '2026-05-03 17:00:00',
            'screen': '2560x1440', 'image': '1024x576',
            'results': [], 'method_results': [
                {'target': 'Start', 'method': 'point_and_act',
                 'strategy': 'taskbar_pre_check', 'error': 12, 'time': 0.6},
            ],
        }
        md = render_baseline_md(out)
        self.assertIn('## Methods', md)
        self.assertIn('`point_and_act`', md)
        self.assertIn('## Per-target winners', md)
        self.assertIn('Start', md)

    def test_renders_strategy_section(self):
        out = {
            'timestamp': '2026-05-03 17:00:00',
            'screen': '2560x1440', 'image': '1024x576',
            'results': [
                {'target': 'A', 'strategy': 'describe_first', 'error': 10, 'time': 1.0},
            ],
            'method_results': [],
        }
        md = render_baseline_md(out)
        self.assertIn('## Prompt strategies', md)
        self.assertIn('`describe_first`', md)

    def test_handles_missing_optional_fields(self):
        """A row missing 'strategy' shouldn't crash render."""
        out = {
            'timestamp': '?', 'screen': '?', 'image': '?',
            'results': [], 'method_results': [
                {'target': 'X', 'method': 'm', 'error': 5, 'time': 0.1},
            ],
        }
        md = render_baseline_md(out)
        self.assertIn('`?`', md, "missing strategy should fall back to '?'")


if __name__ == '__main__':
    unittest.main()
