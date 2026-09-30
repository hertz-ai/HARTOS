"""Acceptance criteria are CHECKED, by the one evaluator that scores success.

Owner rule (2026-09-24, relayed by cc12): every task is broken into checks
that can be verified approximately deterministically, as
{what, check, expected, tolerance, derivation}.

Measured before this change (read, not grepped): AgentAction carried two
success vocabularies. `expected_outcome` ({metric: value}) WAS evaluated by
_compute_success_score (10 % relative / 0.05 absolute tolerance) and fed
HevolveAI's success signal. `acceptance_criteria` (List[str]) was stored and
serialized and NEVER evaluated, although its three callers already write
near-predicates: 'hive_score >= 0.83', 'health_score > baseline',
'all_shards_complete', 'optimizer_health_score > 0'.

Now one evaluator serves both: a criterion is
{kind: metric|test|invariant, target, op, value, tolerance, derivation};
legacy strings are parsed into it; expected_outcome is the degenerate
`metric ==` criterion with the old tolerance. A criterion whose target was
never measured is `unevaluable`: visible in the chain, never counted as a pass.
The value is read from the latest OBSERVATION (the world's report) before the
outcome (the agent's own report).
"""
import unittest
from unittest.mock import MagicMock, patch

from integrations.agent_engine.agent_attribution import (
    AgentAction, AgentAttributionOrchestrator, Observation,
    evaluate_criterion, parse_criterion)


def _action(expected=None, criteria=None, outcome=None, observations=None):
    return AgentAction(
        action_id='a', goal_id='g', agent_id='x', action_type='y',
        started_at=0, expected_outcome=expected or {},
        acceptance_criteria=criteria or [], outcome=outcome or {},
        observations=observations or [])


class TestParsing(unittest.TestCase):
    def test_a_comparison_string_becomes_a_metric(self):
        c = parse_criterion('hive_score >= 0.83')
        self.assertEqual((c['kind'], c['target'], c['op'], c['value']),
                         ('metric', 'hive_score', '>=', 0.83))

    def test_a_bare_name_becomes_a_test(self):
        c = parse_criterion('all_shards_complete')
        self.assertEqual((c['kind'], c['target']), ('test', 'all_shards_complete'))

    def test_a_named_right_hand_side_is_a_reference(self):
        c = parse_criterion('health_score > baseline')
        self.assertEqual((c['target'], c['op'], c['value_ref']),
                         ('health_score', '>', 'baseline'))

    def test_free_text_is_kept_but_unevaluable(self):
        c = parse_criterion('the answer should feel natural')
        v = evaluate_criterion(c, {}, [])
        self.assertEqual(v['verdict'], 'unevaluable')

    def test_a_structured_criterion_passes_through(self):
        raw = {'kind': 'invariant', 'target': 'rows_out', 'op': '==',
               'value_ref': 'rows_in', 'derivation': 'filter keeps every row'}
        self.assertEqual(parse_criterion(raw)['derivation'],
                         'filter keeps every row')


class TestEvaluation(unittest.TestCase):
    def test_metric_pass_and_fail(self):
        c = parse_criterion('hive_score >= 0.83')
        self.assertEqual(evaluate_criterion(c, {'hive_score': 0.9}, [])['verdict'], 'pass')
        self.assertEqual(evaluate_criterion(c, {'hive_score': 0.5}, [])['verdict'], 'fail')

    def test_the_boundary_is_inclusive_for_ge(self):
        c = parse_criterion('hive_score >= 0.83')
        self.assertEqual(evaluate_criterion(c, {'hive_score': 0.83}, [])['verdict'], 'pass')

    def test_a_missing_measurement_is_unevaluable_not_fail(self):
        c = parse_criterion('hive_score >= 0.83')
        self.assertEqual(evaluate_criterion(c, {}, [])['verdict'], 'unevaluable')

    def test_a_reference_is_resolved_from_the_same_evidence(self):
        c = parse_criterion('health_score > baseline')
        self.assertEqual(evaluate_criterion(
            c, {'health_score': 0.7, 'baseline': 0.6}, [])['verdict'], 'pass')
        self.assertEqual(evaluate_criterion(
            c, {'health_score': 0.5, 'baseline': 0.6}, [])['verdict'], 'fail')

    def test_a_test_criterion_reads_truthiness(self):
        c = parse_criterion('all_shards_complete')
        self.assertEqual(evaluate_criterion(c, {'all_shards_complete': True}, [])['verdict'], 'pass')
        self.assertEqual(evaluate_criterion(c, {'all_shards_complete': False}, [])['verdict'], 'fail')

    def test_equality_uses_the_stated_tolerance(self):
        c = parse_criterion({'kind': 'metric', 'target': 'x', 'op': '==',
                             'value': 1.0, 'tolerance': 0.01})
        self.assertEqual(evaluate_criterion(c, {'x': 1.005}, [])['verdict'], 'pass')
        self.assertEqual(evaluate_criterion(c, {'x': 1.02}, [])['verdict'], 'fail')

    def test_the_world_observation_beats_the_agent_claim(self):
        c = parse_criterion('hive_score >= 0.83')
        obs = [Observation(timestamp=1.0, observation_type='world_state',
                           data={'hive_score': 0.4}, source='probe')]
        v = evaluate_criterion(c, {'hive_score': 0.99}, obs)
        self.assertEqual((v['verdict'], v['source']), ('fail', 'observation'))


class TestSuccessScoreUsesTheOneEvaluator(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()

    def test_criteria_alone_now_decide_the_score(self):
        """Before: criteria were ignored and this read a neutral 0.5."""
        ok = _action(criteria=['hive_score >= 0.8'], outcome={'hive_score': 0.9})
        bad = _action(criteria=['hive_score >= 0.8'], outcome={'hive_score': 0.1})
        self.assertEqual(self.orch._compute_success_score(ok), 1.0)
        self.assertEqual(self.orch._compute_success_score(bad), 0.0)

    def test_unmeasured_criteria_leave_the_neutral_score(self):
        a = _action(criteria=['benchmark_prover_reachable'], outcome={})
        self.assertEqual(self.orch._compute_success_score(a), 0.5)

    def test_expected_and_criteria_combine(self):
        a = _action(expected={'status': 'completed'},
                    criteria=['hive_score >= 0.8'],
                    outcome={'status': 'completed', 'hive_score': 0.1})
        self.assertEqual(self.orch._compute_success_score(a), 0.5)

    def test_error_still_scores_zero_whatever_the_criteria(self):
        a = _action(criteria=['hive_score >= 0.8'],
                    outcome={'status': 'error', 'hive_score': 0.9})
        self.assertEqual(self.orch._compute_success_score(a), 0.0)

    def test_the_legacy_expected_tolerance_is_unchanged(self):
        """expected_outcome is the degenerate metric criterion: the old
        10 % relative / 0.05 absolute band still decides it."""
        within = _action(expected={'score': 0.8}, outcome={'score': 0.79})
        near_zero = _action(expected={'delta': 0.0}, outcome={'delta': 0.03})
        off = _action(expected={'score': 0.8}, outcome={'score': 0.3})
        self.assertEqual(self.orch._compute_success_score(within), 1.0)
        self.assertEqual(self.orch._compute_success_score(near_zero), 1.0)
        self.assertEqual(self.orch._compute_success_score(off), 0.0)


class TestVerdictsReachTheLearningChain(unittest.TestCase):
    def test_each_criterion_verdict_is_in_the_submitted_chain(self):
        orch = AgentAttributionOrchestrator()
        a = _action(criteria=['hive_score >= 0.8', 'all_shards_complete'],
                    outcome={'hive_score': 0.9})
        a.completed_at = 1.0
        bridge = MagicMock()
        with patch('integrations.agent_engine.world_model_bridge.'
                   'get_world_model_bridge', return_value=bridge):
            orch._submit_to_world_model(a)
        chain = bridge.record_interaction.call_args.kwargs['attribution_chain']
        verdicts = {v['target']: v['verdict'] for v in chain['criteria_verdicts']}
        self.assertEqual(verdicts, {'hive_score': 'pass',
                                    'all_shards_complete': 'unevaluable'})


class TestTheCallersCriteriaAreCheckableAgainstWhatTheyReport(unittest.TestCase):
    """Measured before: none of the three callers' criteria named a key their
    own complete_action outcome carried, so every one would read
    'unevaluable'.  The outcome shapes below are the ones the callers report
    (hive_benchmark_prover complete_action, compute_optimizer complete_action);
    the criteria are the callers' own."""

    def _criteria_of(self, path, anchor):
        import ast, os
        src = open(os.path.join(os.path.dirname(__file__), '..', '..', path),
                   encoding='utf-8').read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'begin_action':
                kw = {k.arg: k.value for k in node.keywords}
                if anchor in ast.dump(kw.get('agent_id')):
                    return kw['acceptance_criteria']
        raise AssertionError(f'no begin_action for {anchor} in {path}')

    def test_benchmark_score_criterion_is_evaluated(self):
        crit = {'kind': 'metric', 'target': 'score', 'op': '>=', 'value': 0.8}
        node = self._criteria_of('integrations/agent_engine/hive_benchmark_prover.py',
                                 'benchmark_prover')
        self.assertIn("'score'", ast_dump := __import__('ast').dump(node))
        v = evaluate_criterion(crit, {'status': 'completed', 'score': 0.85,
                                      'num_nodes': 3}, [])
        self.assertEqual(v['verdict'], 'pass')

    def test_optimizer_invariant_is_evaluated(self):
        node = self._criteria_of('core/compute_optimizer.py', 'compute_optimizer')
        crit = __import__('ast').literal_eval(node)[0]
        outcome = {'status': 'completed', 'baseline_health_score': 0.6,
                   'final_health_score': 0.55, 'health_score_delta': -0.05}
        self.assertEqual(evaluate_criterion(crit, outcome, [])['verdict'], 'fail')
        outcome['final_health_score'] = 0.62
        self.assertEqual(evaluate_criterion(crit, outcome, [])['verdict'], 'pass')
