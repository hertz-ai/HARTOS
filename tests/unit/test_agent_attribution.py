"""
Tests for AgentAttributionOrchestrator — the single converging path
for agent action attribution.

Validates:
  1. begin_action → record_step → complete_action lifecycle
  2. Credit assignment: exponential decay across steps
  3. Success score computation vs expected_outcome
  4. Thread safety under concurrent begin/record/complete
  5. TTL cleanup of expired actions
  6. MAX_OPEN_ACTIONS eviction
  7. MAX_STEPS_PER_ACTION cap
  8. WorldModelBridge integration (via mock — no live HevolveAI needed)
  9. EventBus emission on completion
 10. Convenience wrappers (begin_action, record_step, complete_action)
"""
import threading
import unittest
from unittest.mock import patch, MagicMock

from integrations.agent_engine.agent_attribution import (
    AgentAttributionOrchestrator,
    AgentAction,
    ActionStep,
    begin_action,
    record_step,
    complete_action,
    get_attribution,
    ACTION_TTL_SECONDS,
    MAX_OPEN_ACTIONS,
    MAX_STEPS_PER_ACTION,
)


class TestBeginAction(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()

    def test_returns_uuid_action_id(self):
        aid = self.orch.begin_action('test_agent', 'test_type')
        self.assertTrue(len(aid) == 36)  # UUID length
        self.assertIn('-', aid)

    def test_stores_goal_id(self):
        aid = self.orch.begin_action('a', 't', goal_id='goal-1')
        action = self.orch.get_action(aid)
        self.assertEqual(action.goal_id, 'goal-1')

    def test_stores_expected_outcome(self):
        aid = self.orch.begin_action('a', 't',
                                     expected_outcome={'score': 0.8})
        action = self.orch.get_action(aid)
        self.assertEqual(action.expected_outcome, {'score': 0.8})

    def test_stores_acceptance_criteria(self):
        aid = self.orch.begin_action('a', 't',
                                     acceptance_criteria=['c1', 'c2'])
        action = self.orch.get_action(aid)
        self.assertEqual(action.acceptance_criteria, ['c1', 'c2'])

    def test_increments_stats(self):
        before = self.orch.get_stats()['total_begun']
        self.orch.begin_action('a', 't')
        self.assertEqual(self.orch.get_stats()['total_begun'], before + 1)


class TestRecordStep(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()
        self.aid = self.orch.begin_action('a', 't')

    def test_records_step(self):
        result = self.orch.record_step(self.aid, 'step 1',
                                        state={'x': 1}, decision='go',
                                        confidence=0.9)
        self.assertTrue(result)
        action = self.orch.get_action(self.aid)
        self.assertEqual(len(action.steps), 1)
        self.assertEqual(action.steps[0].description, 'step 1')
        self.assertEqual(action.steps[0].decision, 'go')
        self.assertEqual(action.steps[0].confidence, 0.9)

    def test_unknown_action_returns_false(self):
        result = self.orch.record_step('fake-id', 'step')
        self.assertFalse(result)

    def test_completed_action_rejects_steps(self):
        self.orch.complete_action(self.aid, outcome={})
        result = self.orch.record_step(self.aid, 'step')
        self.assertFalse(result)

    def test_confidence_clamped_to_range(self):
        # Use fresh action (self.aid is set up but reused — clamp test on fresh)
        aid2 = self.orch.begin_action('a', 't')
        self.orch.record_step(aid2, 's', confidence=2.0)
        self.orch.record_step(aid2, 's2', confidence=-0.5)
        action = self.orch.get_action(aid2)
        self.assertIsNotNone(action)
        self.assertEqual(action.steps[0].confidence, 1.0)
        self.assertEqual(action.steps[1].confidence, 0.0)

    def test_max_steps_per_action_enforced(self):
        aid = self.orch.begin_action('a', 't')
        # Record MAX steps + 1
        for i in range(MAX_STEPS_PER_ACTION):
            self.orch.record_step(aid, f's{i}')
        # One more should fail
        result = self.orch.record_step(aid, 'overflow')
        self.assertFalse(result)
        action = self.orch.get_action(aid)
        self.assertEqual(len(action.steps), MAX_STEPS_PER_ACTION)


class TestCompleteAction(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()

    @patch('integrations.agent_engine.agent_attribution.'
           'AgentAttributionOrchestrator._submit_to_world_model')
    @patch('integrations.agent_engine.agent_attribution.'
           'AgentAttributionOrchestrator._emit_completion_event')
    def test_complete_action_removes_from_open(self, _emit, _submit):
        aid = self.orch.begin_action('a', 't')
        self.orch.record_step(aid, 's1')
        result = self.orch.complete_action(aid, outcome={'status': 'completed'})
        self.assertTrue(result)
        self.assertIsNone(self.orch.get_action(aid))

    def test_unknown_action_returns_false(self):
        result = self.orch.complete_action('fake-id')
        self.assertFalse(result)

    @patch('integrations.agent_engine.agent_attribution.'
           'AgentAttributionOrchestrator._submit_to_world_model')
    @patch('integrations.agent_engine.agent_attribution.'
           'AgentAttributionOrchestrator._emit_completion_event')
    def test_complete_increments_stats(self, _emit, _submit):
        before = self.orch.get_stats()['total_completed']
        aid = self.orch.begin_action('a', 't')
        self.orch.complete_action(aid)
        self.assertEqual(self.orch.get_stats()['total_completed'], before + 1)


class TestCreditAssignment(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()

    def test_empty_steps_returns_empty(self):
        action = AgentAction(
            action_id='a', goal_id=None, agent_id='x',
            action_type='y', started_at=0,
        )
        credits = self.orch._compute_credit_assignment(action)
        self.assertEqual(credits, {})

    def test_credits_sum_to_one(self):
        action = AgentAction(
            action_id='a', goal_id=None, agent_id='x',
            action_type='y', started_at=0,
            steps=[ActionStep(timestamp=i, description=f's{i}')
                   for i in range(5)],
        )
        credits = self.orch._compute_credit_assignment(action)
        total = sum(credits.values())
        self.assertAlmostEqual(total, 1.0, places=3)

    def test_later_steps_get_more_credit(self):
        action = AgentAction(
            action_id='a', goal_id=None, agent_id='x',
            action_type='y', started_at=0,
            steps=[ActionStep(timestamp=i, description=f's{i}')
                   for i in range(5)],
        )
        credits = self.orch._compute_credit_assignment(action)
        # Last step (index 4) should have highest credit
        self.assertGreater(credits[4], credits[0])


class TestSuccessScore(unittest.TestCase):
    def setUp(self):
        self.orch = AgentAttributionOrchestrator()

    def _make_action(self, expected=None, outcome=None):
        return AgentAction(
            action_id='a', goal_id=None, agent_id='x',
            action_type='y', started_at=0,
            expected_outcome=expected or {},
            outcome=outcome or {},
        )

    def test_no_expectation_returns_neutral(self):
        action = self._make_action(outcome={})
        score = self.orch._compute_success_score(action)
        self.assertEqual(score, 0.5)

    def test_error_outcome_returns_zero(self):
        action = self._make_action(outcome={'status': 'error'})
        self.assertEqual(self.orch._compute_success_score(action), 0.0)
        action2 = self._make_action(outcome={'error': 'boom'})
        self.assertEqual(self.orch._compute_success_score(action2), 0.0)

    def test_timeout_returns_low(self):
        action = self._make_action(outcome={'status': 'timeout'})
        self.assertEqual(self.orch._compute_success_score(action), 0.2)

    def test_matching_expected_returns_high(self):
        action = self._make_action(
            expected={'score': 0.8},
            outcome={'score': 0.79},  # within 10% tolerance
        )
        self.assertGreater(self.orch._compute_success_score(action), 0.9)

    def test_mismatched_expected_returns_low(self):
        action = self._make_action(
            expected={'score': 0.8},
            outcome={'score': 0.3},  # way off
        )
        self.assertLess(self.orch._compute_success_score(action), 0.5)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_begin_record_complete(self):
        orch = AgentAttributionOrchestrator()
        errors = []

        def worker():
            try:
                for _ in range(20):
                    aid = orch.begin_action('a', 't')
                    for i in range(5):
                        orch.record_step(aid, f's{i}')
                    with patch.object(orch, '_submit_to_world_model'), \
                         patch.object(orch, '_emit_completion_event'):
                        orch.complete_action(aid, outcome={'status': 'ok'})
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        stats = orch.get_stats()
        self.assertEqual(stats['total_begun'], 160)
        self.assertEqual(stats['total_completed'], 160)
        self.assertEqual(stats['open_count'], 0)


class TestTTLCleanup(unittest.TestCase):
    def test_cleanup_expired_actions(self):
        orch = AgentAttributionOrchestrator()
        import time as _t
        aid = orch.begin_action('a', 't')
        # Artificially age the action
        orch._open_actions[aid].started_at = _t.time() - ACTION_TTL_SECONDS - 10

        with patch.object(orch, '_submit_to_world_model'), \
             patch.object(orch, '_emit_completion_event'):
            count = orch.cleanup_expired()

        self.assertEqual(count, 1)
        self.assertIsNone(orch.get_action(aid))
        self.assertEqual(orch.get_stats()['total_timed_out'], 1)


class TestWorldModelBridgeIntegration(unittest.TestCase):
    """Validate that complete_action routes to WorldModelBridge.record_interaction."""

    def test_submit_calls_record_interaction(self):
        orch = AgentAttributionOrchestrator()
        mock_bridge = MagicMock()
        mock_get_bridge = MagicMock(return_value=mock_bridge)

        aid = orch.begin_action('benchmark_prover', 'benchmark_run',
                                goal_id='g1',
                                expected_outcome={'score': 0.7})
        orch.record_step(aid, 'dispatch', state={'nodes': 3}, decision='start')
        orch.record_step(aid, 'shard_1_done', decision='continue', confidence=0.8)

        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                   mock_get_bridge):
            with patch.object(orch, '_emit_completion_event'):
                orch.complete_action(aid, outcome={
                    'status': 'completed', 'score': 0.75,
                })

        mock_bridge.record_interaction.assert_called_once()
        call_kwargs = mock_bridge.record_interaction.call_args.kwargs
        self.assertEqual(call_kwargs['user_id'], 'benchmark_prover')
        self.assertEqual(call_kwargs['goal_id'], 'g1')
        # prompt contains the attribution chain JSON
        import json
        chain = json.loads(call_kwargs['prompt'])
        self.assertEqual(chain['agent_id'], 'benchmark_prover')
        self.assertEqual(chain['action_type'], 'benchmark_run')
        self.assertEqual(chain['step_count'], 2)
        self.assertIn('step_credits', chain)
        self.assertIn('success_score', chain)


class TestEventBusEmission(unittest.TestCase):
    def test_completion_emits_event(self):
        orch = AgentAttributionOrchestrator()
        mock_emit = MagicMock()

        aid = orch.begin_action('a', 't')
        with patch('core.platform.events.emit_event', mock_emit), \
             patch.object(orch, '_submit_to_world_model'):
            orch.complete_action(aid, outcome={'status': 'ok'})

        mock_emit.assert_called()
        args = mock_emit.call_args[0]
        self.assertEqual(args[0], 'agent.action.completed')
        payload = args[1]
        self.assertEqual(payload['agent_id'], 'a')
        self.assertEqual(payload['action_type'], 't')


class TestConvenienceWrappers(unittest.TestCase):
    def test_module_level_functions_route_to_singleton(self):
        # Clear the singleton
        import integrations.agent_engine.agent_attribution as aa
        aa._orchestrator = None

        aid = begin_action('agent_x', 'type_y', goal_id='g42')
        self.assertTrue(record_step(aid, 'first step'))

        with patch.object(get_attribution(), '_submit_to_world_model'), \
             patch.object(get_attribution(), '_emit_completion_event'):
            self.assertTrue(complete_action(aid, outcome={}))

        stats = get_attribution().get_stats()
        self.assertGreaterEqual(stats['total_begun'], 1)
        self.assertGreaterEqual(stats['total_completed'], 1)


class TestSingleton(unittest.TestCase):
    def test_get_attribution_returns_same_instance(self):
        import integrations.agent_engine.agent_attribution as aa
        aa._orchestrator = None
        a = get_attribution()
        b = get_attribution()
        self.assertIs(a, b)


if __name__ == '__main__':
    unittest.main()
