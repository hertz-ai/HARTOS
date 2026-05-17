"""Tests for RSI-5: non-deterministic exploration arm.

Contract:
    1. select_strategy() returns 'exploit' whenever the feature flag
       is off — callers stay on the deterministic LLM hypothesis path.
    2. With the flag on, strategy follows ε-greedy: rng.random() < ε
       picks 'explore', else 'exploit'.
    3. weighted_sample falls back to uniform when weights are missing,
       wrong length, or sum to zero — never raises, never returns None
       unless the candidate list is empty.
    4. pick_exploration_candidate prefers attribution priors, then
       caller fallback, else None.
"""
import os
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestStrategySelection(unittest.TestCase):
    def setUp(self):
        os.environ.pop('HEVOLVE_RSI_EXPLORE', None)
        os.environ.pop('HEVOLVE_RSI_EPSILON', None)

    def tearDown(self):
        os.environ.pop('HEVOLVE_RSI_EXPLORE', None)
        os.environ.pop('HEVOLVE_RSI_EPSILON', None)

    def test_flag_off_always_exploit(self):
        from integrations.agent_engine.exploration_arm import select_strategy
        rng = random.Random(0)
        for _ in range(100):
            self.assertEqual(select_strategy(rng), 'exploit')

    def test_flag_on_epsilon_zero_always_exploit(self):
        from integrations.agent_engine.exploration_arm import select_strategy
        os.environ['HEVOLVE_RSI_EXPLORE'] = '1'
        os.environ['HEVOLVE_RSI_EPSILON'] = '0'
        rng = random.Random(0)
        for _ in range(100):
            self.assertEqual(select_strategy(rng), 'exploit')

    def test_flag_on_epsilon_one_always_explore(self):
        from integrations.agent_engine.exploration_arm import select_strategy
        os.environ['HEVOLVE_RSI_EXPLORE'] = '1'
        os.environ['HEVOLVE_RSI_EPSILON'] = '1'
        rng = random.Random(0)
        for _ in range(100):
            self.assertEqual(select_strategy(rng), 'explore')

    def test_flag_on_epsilon_half_mix(self):
        from integrations.agent_engine.exploration_arm import select_strategy
        os.environ['HEVOLVE_RSI_EXPLORE'] = '1'
        os.environ['HEVOLVE_RSI_EPSILON'] = '0.5'
        rng = random.Random(42)
        counts = {'explore': 0, 'exploit': 0}
        for _ in range(1000):
            counts[select_strategy(rng)] += 1
        # With seed 42 and ε=0.5 we expect ~500 of each; allow 20% band.
        self.assertGreater(counts['explore'], 400)
        self.assertGreater(counts['exploit'], 400)

    def test_invalid_epsilon_falls_back_to_default(self):
        from integrations.agent_engine.exploration_arm import (
            select_strategy, _epsilon,
        )
        os.environ['HEVOLVE_RSI_EXPLORE'] = '1'
        os.environ['HEVOLVE_RSI_EPSILON'] = 'not-a-number'
        self.assertAlmostEqual(_epsilon(), 0.1)
        # And select_strategy still works.
        self.assertIn(select_strategy(random.Random(0)),
                      ('explore', 'exploit'))


class TestWeightedSample(unittest.TestCase):
    def test_empty_returns_none(self):
        from integrations.agent_engine.exploration_arm import weighted_sample
        self.assertIsNone(weighted_sample([]))

    def test_no_weights_uniform(self):
        from integrations.agent_engine.exploration_arm import weighted_sample
        rng = random.Random(0)
        self.assertIn(weighted_sample(['a', 'b', 'c'], rng=rng),
                      ('a', 'b', 'c'))

    def test_length_mismatch_falls_back_to_uniform(self):
        from integrations.agent_engine.exploration_arm import weighted_sample
        rng = random.Random(0)
        pick = weighted_sample(['a', 'b', 'c'], weights=[1.0, 2.0], rng=rng)
        self.assertIn(pick, ('a', 'b', 'c'))

    def test_zero_weight_falls_back_to_uniform(self):
        from integrations.agent_engine.exploration_arm import weighted_sample
        rng = random.Random(0)
        pick = weighted_sample(['a', 'b'], weights=[0.0, 0.0], rng=rng)
        self.assertIn(pick, ('a', 'b'))

    def test_heavy_weight_is_preferred(self):
        from integrations.agent_engine.exploration_arm import weighted_sample
        rng = random.Random(0)
        counts = {'a': 0, 'b': 0}
        for _ in range(2000):
            counts[weighted_sample(['a', 'b'], weights=[1.0, 99.0],
                                    rng=rng)] += 1
        self.assertGreater(counts['b'], counts['a'] * 10)


class TestPickExplorationCandidate(unittest.TestCase):
    def test_uses_attribution_when_present(self):
        from integrations.agent_engine import exploration_arm
        with patch.object(
            exploration_arm,
            'usage_priors_from_attribution',
            return_value=(['m1', 'm2'], [1.0, 99.0]),
        ):
            rng = random.Random(0)
            counts = {'m1': 0, 'm2': 0}
            for _ in range(500):
                counts[exploration_arm.pick_exploration_candidate(
                    'some_tool', rng=rng)] += 1
            self.assertGreater(counts['m2'], counts['m1'] * 5)

    def test_falls_back_to_caller_pool(self):
        from integrations.agent_engine import exploration_arm
        with patch.object(
            exploration_arm,
            'usage_priors_from_attribution',
            return_value=([], []),
        ):
            pick = exploration_arm.pick_exploration_candidate(
                'tool_x', fallback_candidates=['x', 'y'],
                rng=random.Random(0),
            )
            self.assertIn(pick, ('x', 'y'))

    def test_none_when_no_priors_and_no_fallback(self):
        from integrations.agent_engine import exploration_arm
        with patch.object(
            exploration_arm,
            'usage_priors_from_attribution',
            return_value=([], []),
        ):
            self.assertIsNone(
                exploration_arm.pick_exploration_candidate('tool_x'))


class TestAttributionPriorResilience(unittest.TestCase):
    def test_unavailable_returns_empty(self):
        from integrations.agent_engine import exploration_arm

        # Shadow the real module to force ImportError inside the function.
        real_import = __builtins__['__import__'] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _blocked(name, *args, **kwargs):
            if name == 'integrations.agent_engine.agent_attribution':
                raise ImportError('module missing')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=_blocked):
            keys, weights = exploration_arm.usage_priors_from_attribution('t')
        self.assertEqual(keys, [])
        self.assertEqual(weights, [])

    def test_bad_snapshot_shape_returns_empty(self):
        from integrations.agent_engine import exploration_arm
        fake_orch = type('O', (), {
            'get_usage_snapshot': staticmethod(lambda: 'not a dict'),
        })
        with patch.object(
            exploration_arm,
            'usage_priors_from_attribution',
            wraps=exploration_arm.usage_priors_from_attribution,
        ), patch(
            'integrations.agent_engine.agent_attribution.'
            'AgentAttributionOrchestrator',
            return_value=fake_orch,
        ):
            keys, weights = exploration_arm.usage_priors_from_attribution('t')
        self.assertEqual(keys, [])


if __name__ == '__main__':
    unittest.main()
