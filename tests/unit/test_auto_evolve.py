"""
Tests for Auto Evolve Orchestrator — democratic thought experiment dispatch.

Tests cover:
- EvolveSession state management
- Singleton pattern
- Owner pause/resume (ownership enforcement)
- Constitutional filter integration
- Vote ranking
- Tool registration
- API endpoint wiring
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestEvolveSession(unittest.TestCase):
    """Test EvolveSession dataclass."""

    def test_default_state(self):
        from integrations.agent_engine.auto_evolve import EvolveSession
        session = EvolveSession()
        self.assertEqual(session.status, 'pending')
        self.assertEqual(session.candidates, 0)
        self.assertEqual(session.dispatched, 0)
        self.assertIsInstance(session.experiments, list)
        self.assertIsInstance(session.errors, list)

    def test_to_dict(self):
        from integrations.agent_engine.auto_evolve import EvolveSession
        session = EvolveSession()
        session.status = 'running'
        session.candidates = 10
        session.selected = 3
        d = session.to_dict()
        self.assertEqual(d['status'], 'running')
        self.assertEqual(d['candidates'], 10)
        self.assertEqual(d['selected'], 3)
        self.assertIn('session_id', d)
        self.assertIn('elapsed_s', d)


class TestSingleton(unittest.TestCase):
    """Test singleton pattern."""

    def test_get_auto_evolve_orchestrator(self):
        from integrations.agent_engine.auto_evolve import get_auto_evolve_orchestrator
        o1 = get_auto_evolve_orchestrator()
        o2 = get_auto_evolve_orchestrator()
        self.assertIs(o1, o2)

    def test_get_status_idle(self):
        from integrations.agent_engine.auto_evolve import AutoEvolveOrchestrator
        orch = AutoEvolveOrchestrator()
        status = orch.get_status()
        self.assertEqual(status['status'], 'idle')


class TestOwnerPauseResume(unittest.TestCase):
    """Test owner-only pause/resume of experiment evolution."""

    def test_pause_nonexistent_experiment(self):
        from integrations.agent_engine.auto_evolve import pause_experiment_evolution
        with patch('integrations.social.models.get_db') as mock_db:
            db = MagicMock()
            mock_db.return_value = db
            with patch('integrations.social.thought_experiment_service.'
                       'ThoughtExperimentService.get_experiment_detail',
                       return_value=None):
                result = pause_experiment_evolution('bad_id', 'user1')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'not_found')

    def test_pause_not_owner(self):
        from integrations.agent_engine.auto_evolve import pause_experiment_evolution
        with patch('integrations.social.models.get_db') as mock_db:
            db = MagicMock()
            mock_db.return_value = db
            with patch('integrations.social.thought_experiment_service.'
                       'ThoughtExperimentService.get_experiment_detail',
                       return_value={'id': 'exp1', 'creator_id': 'owner1'}):
                result = pause_experiment_evolution('exp1', 'not_owner')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'not_owner')

    def test_pause_and_resume_by_owner(self):
        from integrations.agent_engine.auto_evolve import (
            pause_experiment_evolution, resume_experiment_evolution,
            is_experiment_paused, _paused_experiments, _pause_lock)

        # Clean state
        with _pause_lock:
            _paused_experiments.clear()

        with patch('integrations.social.models.get_db') as mock_db:
            db = MagicMock()
            mock_db.return_value = db
            with patch('integrations.social.thought_experiment_service.'
                       'ThoughtExperimentService.get_experiment_detail',
                       return_value={'id': 'exp1', 'creator_id': 'owner1'}):
                # Pause
                result = pause_experiment_evolution('exp1', 'owner1')
                self.assertTrue(result['success'])
                self.assertTrue(is_experiment_paused('exp1'))

                # Resume by owner
                result = resume_experiment_evolution('exp1', 'owner1')
                self.assertTrue(result['success'])
                self.assertFalse(is_experiment_paused('exp1'))

    def test_resume_not_paused(self):
        from integrations.agent_engine.auto_evolve import (
            resume_experiment_evolution, _paused_experiments, _pause_lock)
        with _pause_lock:
            _paused_experiments.clear()
        result = resume_experiment_evolution('exp1', 'user1')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'not_paused')

    def test_resume_wrong_user(self):
        from integrations.agent_engine.auto_evolve import (
            pause_experiment_evolution, resume_experiment_evolution,
            _paused_experiments, _pause_lock)
        with _pause_lock:
            _paused_experiments.clear()

        with patch('integrations.social.models.get_db') as mock_db:
            db = MagicMock()
            mock_db.return_value = db
            with patch('integrations.social.thought_experiment_service.'
                       'ThoughtExperimentService.get_experiment_detail',
                       return_value={'id': 'exp2', 'creator_id': 'owner2'}):
                pause_experiment_evolution('exp2', 'owner2')

        result = resume_experiment_evolution('exp2', 'not_owner2')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'not_owner')

        # Clean up
        with _pause_lock:
            _paused_experiments.clear()

    def test_get_paused_experiments(self):
        from integrations.agent_engine.auto_evolve import (
            get_paused_experiments, _paused_experiments, _pause_lock)
        with _pause_lock:
            _paused_experiments.clear()
            _paused_experiments['exp_a'] = 'user_a'
            _paused_experiments['exp_b'] = 'user_b'

        paused = get_paused_experiments()
        self.assertIn('exp_a', paused)
        self.assertIn('exp_b', paused)

        with _pause_lock:
            _paused_experiments.clear()


class TestIterateHypothesisPauseCheck(unittest.TestCase):
    """Test that iterate_hypothesis respects pause state."""

    def test_iterate_returns_pause_signal(self):
        from integrations.agent_engine.thought_experiment_tools import iterate_hypothesis
        with patch('integrations.agent_engine.auto_evolve.is_experiment_paused',
                   return_value=True):
            result = json.loads(iterate_hypothesis(
                experiment_id='paused_exp',
                hypothesis='test'))
        self.assertFalse(result['success'])
        self.assertTrue(result.get('paused'))

    def test_iterate_proceeds_when_not_paused(self):
        from integrations.agent_engine.thought_experiment_tools import iterate_hypothesis
        with patch('integrations.agent_engine.auto_evolve.is_experiment_paused',
                   return_value=False):
            # Will fail on DB (no real DB) but should NOT return paused
            result = json.loads(iterate_hypothesis(
                experiment_id='active_exp',
                hypothesis='test'))
        self.assertFalse(result.get('paused', False))


class TestToolRegistration(unittest.TestCase):
    """Test tool registration list."""

    def test_auto_evolve_tools_count(self):
        from integrations.agent_engine.auto_evolve import AUTO_EVOLVE_TOOLS
        self.assertEqual(len(AUTO_EVOLVE_TOOLS), 4)

    def test_auto_evolve_tool_names(self):
        from integrations.agent_engine.auto_evolve import AUTO_EVOLVE_TOOLS
        names = [t['name'] for t in AUTO_EVOLVE_TOOLS]
        self.assertIn('start_auto_evolve', names)
        self.assertIn('get_auto_evolve_status', names)
        self.assertIn('pause_evolve_experiment', names)
        self.assertIn('resume_evolve_experiment', names)

    def test_all_tools_have_tags(self):
        from integrations.agent_engine.auto_evolve import AUTO_EVOLVE_TOOLS
        for tool in AUTO_EVOLVE_TOOLS:
            self.assertIn('auto_evolve', tool['tags'])
            self.assertIn('func', tool)
            self.assertTrue(callable(tool['func']))


class TestConstitutionalFilter(unittest.TestCase):
    """Test constitutional filter integration."""

    def test_filter_passes_clean_experiments(self):
        from integrations.agent_engine.auto_evolve import AutoEvolveOrchestrator, EvolveSession
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [
            {'id': '1', 'title': 'Test', 'hypothesis': 'Good idea'},
            {'id': '2', 'title': 'Another', 'hypothesis': 'Also good'},
        ]

        # No ConstitutionalFilter available — all pass through
        approved = orch._constitutional_filter(session, candidates)
        self.assertEqual(len(approved), 2)

    def test_filter_blocks_rejected(self):
        from integrations.agent_engine.auto_evolve import AutoEvolveOrchestrator, EvolveSession
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [
            {'id': '1', 'title': 'Good', 'hypothesis': 'Safe'},
            {'id': '2', 'title': 'Bad', 'hypothesis': 'Blocked'},
        ]

        def mock_check(text):
            if 'Blocked' in text:
                return (False, 'blocked')
            return (True, '')

        with patch('security.hive_guardrails.ConstitutionalFilter.check_prompt',
                   side_effect=mock_check):
            approved = orch._constitutional_filter(session, candidates)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['id'], '1')


class TestVoteRanking(unittest.TestCase):
    """Test vote tally ranking."""

    def test_ranking_by_score(self):
        from integrations.agent_engine.auto_evolve import AutoEvolveOrchestrator, EvolveSession
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [
            {'id': '1'}, {'id': '2'}, {'id': '3'},
        ]

        def mock_tally(db, exp_id):
            scores = {'1': 0.8, '2': 0.2, '3': 1.5}
            return {'weighted_score': scores.get(exp_id, 0)}

        with patch('integrations.social.models.get_db') as mock_db:
            db = MagicMock()
            mock_db.return_value = db
            with patch('integrations.social.thought_experiment_service.'
                       'ThoughtExperimentService.tally_votes',
                       side_effect=mock_tally):
                ranked = orch._rank_by_votes(session, candidates, 0.3)

        # Should be sorted by score desc, filtering out score < 0.3
        self.assertEqual(len(ranked), 2)  # id=2 (0.2) filtered out
        self.assertEqual(ranked[0]['id'], '3')  # highest
        self.assertEqual(ranked[1]['id'], '1')  # second


class TestAutoEvolveToolFunctions(unittest.TestCase):
    """Test the tool wrapper functions."""

    def test_start_auto_evolve_returns_json(self):
        from integrations.agent_engine.auto_evolve import start_auto_evolve
        with patch('integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as mock_orch:
            mock_orch.return_value.start.return_value = {
                'success': True, 'session_id': 'test123'}
            result = json.loads(start_auto_evolve())
        self.assertTrue(result['success'])

    def test_get_auto_evolve_status_returns_json(self):
        from integrations.agent_engine.auto_evolve import get_auto_evolve_status
        with patch('integrations.agent_engine.auto_evolve.get_auto_evolve_orchestrator') as mock_orch:
            mock_orch.return_value.get_status.return_value = {'status': 'idle'}
            result = json.loads(get_auto_evolve_status())
        self.assertEqual(result['status'], 'idle')


if __name__ == '__main__':
    unittest.main()
