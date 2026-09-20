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
from types import SimpleNamespace
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

    def test_selecting_session_cannot_be_started_twice(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        orch._active_session = EvolveSession(status='selecting')
        result = orch.start()
        self.assertFalse(result['success'])


class TestGoalReconciliation(unittest.TestCase):
    def _db_context(self, goals):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = goals
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        return context

    def test_completed_and_failed_goals_close_the_cycle_truthfully(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=2)
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'},
            {'id': 'e2', 'goal_id': 'g2', 'status': 'dispatched'},
        ]
        orch._active_session = session
        goals = [
            SimpleNamespace(id='g1', status='completed'),
            SimpleNamespace(id='g2', status='failed'),
        ]
        with patch('integrations.social.models.db_session',
                   return_value=self._db_context(goals)):
            result = orch.reconcile()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(session.experiments[1]['status'], 'failed')
        self.assertIn('failed', session.experiments[1]['reason'])

    def test_a_paused_goal_keeps_its_experiment_running(self):
        """A pause is a throttle, not an ending.

        This test previously asserted the opposite -- that a paused goal
        "closes the cycle truthfully".  Six throttle and budget paths write
        'paused' while the goal is still alive (agent_daemon, budget_gate,
        goal_manager, coding_daemon), and four readers treat
        ['active', 'paused'] as live.  Closing the experiment on a pause
        left its row at 'evaluating' with no evaluation recorded, so the
        next cycle's candidate gather picked it up again and created a
        SECOND goal for the same experiment, which then ran alongside the
        first when the budget gate released it.
        """
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=1)
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'},
        ]
        orch._active_session = session
        with patch('integrations.social.models.db_session',
                   return_value=self._db_context(
                       [SimpleNamespace(id='g1', status='paused')])):
            result = orch.reconcile()

        self.assertEqual(result['status'], 'running')
        self.assertEqual(session.experiments[0]['status'], 'running')
        self.assertEqual(session.experiments[0]['goal_status'], 'paused')
        self.assertEqual(result['failed'], 0)

    def test_a_cycle_parked_past_its_max_age_closes_so_the_next_can_run(self):
        """The release valve for the change above.

        start() and the agent daemon's tick both refuse to open a new cycle
        while one is 'selecting', 'dispatching' or 'running'.  Once a paused
        goal no longer closes its experiment, an indefinitely parked goal
        would hold the orchestrator open forever and auto-evolve would never
        run again.  Ageing the cycle out is the only thing that releases it.
        """
        import time as _time
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
            AUTO_EVOLVE_SESSION_MAX_AGE_S)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=1)
        session.started_at = _time.time() - (AUTO_EVOLVE_SESSION_MAX_AGE_S + 60)
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'},
        ]
        orch._active_session = session
        with patch('integrations.social.models.db_session',
                   return_value=self._db_context(
                       [SimpleNamespace(id='g1', status='paused')])):
            result = orch.reconcile()

        self.assertEqual(result['status'], 'failed')
        self.assertIn('aged out', session.experiments[0]['reason'])
        self.assertEqual(session.experiments[0]['goal_status'], 'paused')

    def test_a_young_cycle_is_not_aged_out(self):
        """The bound must not fire on a cycle that just started."""
        import time as _time
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=1)
        session.started_at = _time.time()
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'},
        ]
        orch._active_session = session
        with patch('integrations.social.models.db_session',
                   return_value=self._db_context(
                       [SimpleNamespace(id='g1', status='paused')])):
            result = orch.reconcile()
        self.assertEqual(result['status'], 'running')

    def test_active_goal_keeps_the_cycle_running(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=1)
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'},
        ]
        orch._active_session = session
        with patch(
                'integrations.social.models.db_session',
                return_value=self._db_context([
                    SimpleNamespace(id='g1', status='active')])):
            result = orch.reconcile()
        self.assertEqual(result['status'], 'running')
        self.assertEqual(session.experiments[0]['status'], 'running')

    def test_evaluated_experiment_is_not_dispatched_again(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        context = self._db_context([])
        with patch('integrations.social.models.db_session',
                   return_value=context), patch(
                'integrations.social.thought_experiment_service.'
                'ThoughtExperimentService.get_active_experiments',
                return_value=[
                    {'id': 'done', 'agent_evaluations_json': [{'score': 1}]},
                    {'id': 'retry', 'agent_evaluations_json': []},
                ]):
            candidates = orch._gather_candidates(
                EvolveSession(), ['evaluating'])
        self.assertEqual([item['id'] for item in candidates], ['retry'])


class TestDaemonAutoEvolveTick(unittest.TestCase):
    def test_reconciles_every_tick_and_starts_only_on_cadence(self):
        from integrations.agent_engine.agent_daemon import AgentDaemon

        daemon = AgentDaemon.__new__(AgentDaemon)
        daemon._auto_evolve_interval_s = 900
        daemon._next_auto_evolve_at = 0.0
        orchestrator = MagicMock()
        orchestrator.reconcile.return_value = {'status': 'completed'}
        orchestrator.start.return_value = {
            'success': True, 'session_id': 'cycle-1'}
        db_context = MagicMock()
        db_context.__enter__.return_value = MagicMock()
        db_context.__exit__.return_value = False

        with patch('integrations.social.models.db_session',
                   return_value=db_context), patch(
                'integrations.social.thought_experiment_service.'
                'ThoughtExperimentService.advance_due_experiments'), patch(
                'integrations.agent_engine.auto_evolve.'
                'get_auto_evolve_orchestrator',
                return_value=orchestrator), patch(
                'integrations.agent_engine.agent_daemon.time.monotonic',
                side_effect=[100.0, 101.0]):
            daemon._advance_thought_experiments()
            daemon._advance_thought_experiments()

        self.assertEqual(orchestrator.reconcile.call_count, 2)
        orchestrator.start.assert_called_once_with(user_id='system')


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
        """Per PRODUCT_MAP §10 the ranking applies BOTH min_score AND a 2/3
        super-majority gate.  Candidates that pass both are ranked by score.
        """
        from integrations.agent_engine.auto_evolve import AutoEvolveOrchestrator, EvolveSession
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [
            {'id': '1'}, {'id': '2'}, {'id': '3'},
        ]

        def mock_tally(db, exp_id):
            # All three have super-majority (8 for / 2 against = 80%) so
            # only the absolute-score floor of 0.3 differentiates them.
            scores = {'1': 0.8, '2': 0.2, '3': 1.5}
            return {
                'weighted_score': scores.get(exp_id, 0),
                'total_for': 8.0,
                'total_against': 2.0,
            }

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


class TestVoteGateFailsClosed(unittest.TestCase):
    """An unreadable tally is not an approval.

    _rank_by_votes applies the constitutional supermajority gate.  When the
    tally raised -- a locked SQLite database is the documented case, which is
    why _is_sqlite_backend exists -- it returned the candidate list unranked,
    which meant UNGATED: every constitutionally-eligible experiment reached
    dispatch with zero votes counted.  That was already wrong behind the admin
    button and became autonomous once the agent daemon started calling
    start(user_id='system') on a timer.
    """

    def _exp(self, exp_id):
        return {'id': exp_id, 'title': exp_id}

    def test_a_failed_tally_dispatches_nothing(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='selecting')
        candidates = [self._exp('e1'), self._exp('e2')]

        db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        with patch('integrations.social.models.db_session',
                   return_value=context),              patch('integrations.social.thought_experiment_service'
                   '.ThoughtExperimentService.tally_votes',
                   side_effect=RuntimeError('database is locked')):
            ranked = orch._rank_by_votes(session, candidates, 0.3)

        self.assertEqual(
            ranked, [],
            'an unreadable vote tally must not admit unvoted experiments')

    def test_a_working_tally_still_gates_on_score_and_supermajority(self):
        """The failure path must not have broken the path it protects."""
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='selecting')

        tallies = {
            'yes': {'weighted_score': 0.9, 'total_for': 9, 'total_against': 1},
            'no': {'weighted_score': 0.9, 'total_for': 1, 'total_against': 9},
        }
        db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = db
        context.__exit__.return_value = False
        with patch('integrations.social.models.db_session',
                   return_value=context),              patch('integrations.social.thought_experiment_service'
                   '.ThoughtExperimentService.tally_votes',
                   side_effect=lambda _db, exp_id: tallies[exp_id]):
            ranked = orch._rank_by_votes(
                session, [self._exp('yes'), self._exp('no')], 0.3)

        self.assertEqual([e['id'] for e in ranked], ['yes'],
                         'the supermajority gate must still reject "no"')
