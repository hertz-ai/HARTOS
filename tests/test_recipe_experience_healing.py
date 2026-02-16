"""
Tests for Recipe Experience Recording, Self-Healing Dispatcher, and Exception Watcher.

Covers:
- ExceptionCollector: record, buffer, patterns, resolve, stats
- RecipeExperienceRecorder: timers, telemetry, merge, hints
- SelfHealingDispatcher: pattern grouping, goal creation, dedup
- ExceptionWatcher: assign/release, process, severity
- Integration: exception → collector → dispatcher → goal
"""
import os
import sys
import json
import time
import tempfile
import threading
import pytest
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test DB path before imports
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ═══════════════════════════════════════════════════════════════════════════
# ExceptionCollector Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptionCollector:

    def setup_method(self):
        from exception_collector import ExceptionCollector
        ExceptionCollector.reset_instance()
        self.collector = ExceptionCollector.get_instance()

    def teardown_method(self):
        from exception_collector import ExceptionCollector
        ExceptionCollector.reset_instance()

    def test_singleton(self):
        from exception_collector import ExceptionCollector
        c1 = ExceptionCollector.get_instance()
        c2 = ExceptionCollector.get_instance()
        assert c1 is c2

    def test_record_basic(self):
        exc = ValueError("test error")
        self.collector.record(exc, module='test_mod', function='test_fn')

        stats = self.collector.get_stats()
        assert stats['total'] == 1
        assert stats['unresolved'] == 1

    def test_record_with_context(self):
        exc = KeyError("missing_key")
        self.collector.record(
            exc, module='create_recipe', function='get_response',
            user_prompt='123_456', action_id=3,
            context={'flow': 0})

        unresolved = self.collector.get_unresolved()
        assert len(unresolved) == 1
        rec = unresolved[0]
        assert rec.exc_type == 'KeyError'
        assert rec.module == 'create_recipe'
        assert rec.function == 'get_response'
        assert rec.user_prompt == '123_456'
        assert rec.action_id == 3
        assert rec.context == {'flow': 0}

    def test_buffer_overflow(self):
        self.collector._max_buffer = 10
        for i in range(20):
            self.collector.record(ValueError(f"error {i}"))

        stats = self.collector.get_stats()
        assert stats['total'] == 10  # capped at buffer size

    def test_mark_resolved(self):
        self.collector.record(ValueError("e1"))
        self.collector.record(ValueError("e2"))

        unresolved = self.collector.get_unresolved()
        assert len(unresolved) == 2

        self.collector.mark_resolved([unresolved[0].id])
        unresolved2 = self.collector.get_unresolved()
        assert len(unresolved2) == 1
        assert unresolved2[0].exc_message == 'e2'

    def test_mark_pattern_resolved(self):
        self.collector.record(ValueError("e1"), module='mod', function='fn')
        self.collector.record(ValueError("e2"), module='mod', function='fn')
        self.collector.record(KeyError("e3"), module='mod', function='fn2')

        self.collector.mark_pattern_resolved('ValueError::mod::fn')
        unresolved = self.collector.get_unresolved()
        assert len(unresolved) == 1
        assert unresolved[0].exc_type == 'KeyError'

    def test_get_patterns(self):
        for i in range(5):
            self.collector.record(ValueError(f"val {i}"), module='m', function='f')
        for i in range(2):
            self.collector.record(KeyError(f"key {i}"), module='m', function='g')

        patterns = self.collector.get_patterns(min_count=3)
        assert len(patterns) == 1
        assert 'ValueError::m::f' in patterns
        assert len(patterns['ValueError::m::f']) == 5

    def test_get_patterns_all(self):
        self.collector.record(ValueError("v"), module='m', function='f')
        self.collector.record(KeyError("k"), module='m', function='g')

        patterns = self.collector.get_patterns(min_count=1)
        assert len(patterns) == 2

    def test_subscriber_notification(self):
        notifications = []
        self.collector.subscribe(lambda rec: notifications.append(rec))
        self.collector.record(ValueError("test"))
        assert len(notifications) == 1
        assert notifications[0].exc_type == 'ValueError'

    def test_unsubscribe(self):
        cb = lambda rec: None
        self.collector.subscribe(cb)
        self.collector.unsubscribe(cb)
        assert len(self.collector._subscribers) == 0

    def test_stats_top_types(self):
        for _ in range(5):
            self.collector.record(ValueError("v"))
        for _ in range(3):
            self.collector.record(KeyError("k"))

        stats = self.collector.get_stats()
        assert stats['unresolved'] == 8
        top_types = dict(stats['top_exception_types'])
        assert top_types['ValueError'] == 5
        assert top_types['KeyError'] == 3

    def test_record_exception_convenience(self):
        from exception_collector import record_exception
        record_exception(ValueError("conv test"), module='m', function='f')

        stats = self.collector.get_stats()
        assert stats['total'] == 1

    def test_thread_safety(self):
        errors = []

        def record_many(n):
            try:
                for i in range(50):
                    self.collector.record(ValueError(f"thread {n} error {i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stats = self.collector.get_stats()
        assert stats['total'] == 250

    def test_clear(self):
        self.collector.record(ValueError("test"))
        assert self.collector.get_stats()['total'] == 1
        self.collector.clear()
        assert self.collector.get_stats()['total'] == 0


# ═══════════════════════════════════════════════════════════════════════════
# RecipeExperienceRecorder Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestRecipeExperienceRecorder:

    def setup_method(self):
        from recipe_experience import _telemetry, _timers
        _telemetry.clear()
        _timers.clear()

    def test_timer_start_stop(self):
        from recipe_experience import RecipeExperienceRecorder as RER
        RER.start_action_timer('test_session', 1)
        time.sleep(0.05)
        RER.stop_action_timer('test_session', 1, 'completed')

        tel = RER.get_telemetry('test_session')
        assert 1 in tel
        assert len(tel[1]['durations']) == 1
        assert tel[1]['durations'][0] >= 0.04
        assert tel[1]['outcomes'] == ['completed']

    def test_record_dead_end(self):
        from recipe_experience import RecipeExperienceRecorder as RER
        RER.record_dead_end('s1', 1, 'tried path A, got timeout')
        RER.record_dead_end('s1', 1, 'tried path B, permission denied')
        RER.record_dead_end('s1', 1, 'tried path A, got timeout')  # duplicate

        tel = RER.get_telemetry('s1')
        assert len(tel[1]['dead_ends']) == 2  # deduped

    def test_record_fallback(self):
        from recipe_experience import RecipeExperienceRecorder as RER
        RER.record_fallback_used('s1', 1, 'retry with different params', True)
        RER.record_fallback_used('s1', 1, 'skip action', False)

        tel = RER.get_telemetry('s1')
        assert len(tel[1]['fallbacks_used']) == 2
        assert tel[1]['fallbacks_used'][0]['success'] is True

    def test_record_tool_call(self):
        from recipe_experience import RecipeExperienceRecorder as RER
        RER.record_tool_call('s1', 1, 'google_search', True, 2.5)
        RER.record_tool_call('s1', 1, 'google_search', False, 1.0)

        tel = RER.get_telemetry('s1')
        stats = tel[1]['tool_stats']['google_search']
        assert stats['calls'] == 2
        assert stats['successes'] == 1

    def test_merge_experience_into_recipe(self):
        from recipe_experience import RecipeExperienceRecorder as RER

        # Create a temp recipe file
        recipe = {
            'status': 'completed',
            'actions': [
                {'action_id': 1, 'action': 'Do something', 'persona': 'test'},
                {'action_id': 2, 'action': 'Do another', 'persona': 'test'},
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            prompts_dir = os.path.join(tmpdir, 'prompts')
            os.makedirs(prompts_dir)
            recipe_path = os.path.join(prompts_dir, '999_0_recipe.json')
            with open(recipe_path, 'w') as f:
                json.dump(recipe, f)

            # Record telemetry
            RER.start_action_timer('test_merge', 1)
            time.sleep(0.05)
            RER.stop_action_timer('test_merge', 1, 'completed')
            RER.record_dead_end('test_merge', 1, 'path X failed')
            RER.record_fallback_used('test_merge', 1, 'retry', True)

            RER.start_action_timer('test_merge', 2)
            time.sleep(0.03)
            RER.stop_action_timer('test_merge', 2, 'completed')

            # Merge — need to be in the right directory
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                RER.merge_experience_into_recipe('999', 0, 'test_merge')
            finally:
                os.chdir(old_cwd)

            # Verify merged recipe
            with open(recipe_path, 'r') as f:
                merged = json.load(f)

            assert 'experience_meta' in merged
            assert merged['experience_meta']['total_runs'] == 1

            action1 = merged['actions'][0]
            assert 'experience' in action1
            assert action1['experience']['run_count'] == 1
            assert action1['experience']['avg_duration_seconds'] > 0
            assert action1['experience']['success_rate'] == 1.0
            assert 'path X failed' in action1['experience']['dead_ends']
            assert 'retry' in action1['experience']['effective_fallbacks']

    def test_build_experience_hints(self):
        from recipe_experience import build_experience_hints

        recipes = [
            {
                'action': 'Open VS Code',
                'experience': {
                    'dead_ends': ['tried cmd.exe, not found'],
                    'effective_fallbacks': ['use powershell instead'],
                    'avg_duration_seconds': 5.2,
                    'success_rate': 0.9,
                }
            },
            {
                'action': 'Write code',
                # no experience
            },
        ]

        hints = build_experience_hints(recipes)
        assert 'AVOID' in hints
        assert 'tried cmd.exe' in hints
        assert 'powershell' in hints
        assert '5.2s' in hints

    def test_build_experience_hints_empty(self):
        from recipe_experience import build_experience_hints
        hints = build_experience_hints([{'action': 'test'}])
        assert hints == 'No prior experience recorded.'

    def test_cleanup_session(self):
        from recipe_experience import RecipeExperienceRecorder as RER, _telemetry, _timers
        RER.start_action_timer('cleanup_test', 1)
        RER.record_dead_end('cleanup_test', 1, 'path X')

        assert 'cleanup_test' in _timers
        assert 'cleanup_test' in _telemetry

        RER.cleanup_session('cleanup_test')
        assert 'cleanup_test' not in _timers
        assert 'cleanup_test' not in _telemetry


# ═══════════════════════════════════════════════════════════════════════════
# SelfHealingDispatcher Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSelfHealingDispatcher:

    def setup_method(self):
        from exception_collector import ExceptionCollector
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher
        ExceptionCollector.reset_instance()
        SelfHealingDispatcher.reset_instance()

    def teardown_method(self):
        from exception_collector import ExceptionCollector
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher
        ExceptionCollector.reset_instance()
        SelfHealingDispatcher.reset_instance()

    def test_singleton(self):
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher
        d1 = SelfHealingDispatcher.get_instance()
        d2 = SelfHealingDispatcher.get_instance()
        assert d1 is d2

    def test_no_dispatch_below_threshold(self):
        from exception_collector import ExceptionCollector
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher

        collector = ExceptionCollector.get_instance()
        dispatcher = SelfHealingDispatcher.get_instance()
        dispatcher._check_interval = 0  # disable throttle

        # Only 2 occurrences (threshold is 3)
        collector.record(ValueError("v1"), module='m', function='f')
        collector.record(ValueError("v2"), module='m', function='f')

        db = MagicMock()
        count = dispatcher.check_and_dispatch(db)
        assert count == 0

    def test_dispatch_above_threshold(self):
        from exception_collector import ExceptionCollector
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher

        collector = ExceptionCollector.get_instance()
        dispatcher = SelfHealingDispatcher.get_instance()
        dispatcher._check_interval = 0

        # 4 occurrences (threshold is 3)
        for i in range(4):
            collector.record(ValueError(f"v{i}"), module='mymod', function='myfn')

        db = MagicMock()
        # Mock the goal creation — GoalManager is lazily imported inside the method
        with patch('integrations.agent_engine.goal_manager.GoalManager.create_goal') as mock_create:
            mock_create.return_value = {'success': True, 'goal': {'id': 'test'}}
            # Mock _is_already_being_fixed (query for existing self_heal goals)
            db.query.return_value.filter.return_value.all.return_value = []

            count = dispatcher.check_and_dispatch(db)

        assert count == 1
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]['goal_type'] == 'self_heal'
        assert 'ValueError' in call_kwargs[1]['title']
        assert 'mymod' in call_kwargs[1]['title']

    def test_no_duplicate_goals(self):
        from exception_collector import ExceptionCollector
        from integrations.agent_engine.self_healing_dispatcher import SelfHealingDispatcher

        collector = ExceptionCollector.get_instance()
        dispatcher = SelfHealingDispatcher.get_instance()
        dispatcher._check_interval = 0

        for i in range(5):
            collector.record(ValueError(f"v{i}"), module='m', function='f')

        # Simulate existing active self_heal goal for this pattern
        mock_goal = MagicMock()
        mock_goal.config_json = {'pattern_key': 'ValueError::m::f'}

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [mock_goal]

        with patch('integrations.agent_engine.goal_manager.GoalManager.create_goal') as mock_create:
            count = dispatcher.check_and_dispatch(db)

        assert count == 0
        mock_create.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# ExceptionWatcher Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptionWatcher:

    def setup_method(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        from exception_collector import ExceptionCollector
        ExceptionWatcher.reset_instance()
        ExceptionCollector.reset_instance()

    def teardown_method(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        from exception_collector import ExceptionCollector
        ExceptionWatcher.reset_instance()
        ExceptionCollector.reset_instance()

    def test_singleton(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        w1 = ExceptionWatcher.get_instance()
        w2 = ExceptionWatcher.get_instance()
        assert w1 is w2

    def test_assign_release(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        watcher = ExceptionWatcher.get_instance()

        assert not watcher.has_watchers()
        watcher.assign_watcher('user1', 'alice')
        assert watcher.has_watchers()
        assert watcher.get_watcher_count() == 1

        watcher.release_watcher('user1')
        assert not watcher.has_watchers()

    def test_assign_idempotent(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        watcher = ExceptionWatcher.get_instance()

        watcher.assign_watcher('user1', 'alice')
        watcher.assign_watcher('user1', 'alice')  # duplicate
        assert watcher.get_watcher_count() == 1

    def test_severity_classification(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        from exception_collector import ExceptionRecord

        watcher = ExceptionWatcher.get_instance()

        critical_rec = ExceptionRecord('MemoryError', 'out of memory')
        assert watcher._classify_severity(critical_rec) == 'critical'

        high_rec = ExceptionRecord('KeyError', 'missing key')
        assert watcher._classify_severity(high_rec) == 'high'

        low_rec = ExceptionRecord('UserWarning', 'minor issue')
        assert watcher._classify_severity(low_rec) == 'low'

    def test_severity_from_message(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        from exception_collector import ExceptionRecord

        watcher = ExceptionWatcher.get_instance()

        fatal_rec = ExceptionRecord('CustomError', 'fatal crash detected')
        assert watcher._classify_severity(fatal_rec) == 'critical'

        failed_rec = ExceptionRecord('CustomError', 'operation failed')
        assert watcher._classify_severity(failed_rec) == 'high'

    def test_watcher_stats(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        watcher = ExceptionWatcher.get_instance()

        watcher.assign_watcher('u1', 'alice')
        watcher.assign_watcher('u2', 'bob')

        stats = watcher.get_watcher_stats()
        assert stats['active_watchers'] == 2
        assert len(stats['watchers']) == 2

    def test_release_all(self):
        from integrations.agent_engine.exception_watcher import ExceptionWatcher
        watcher = ExceptionWatcher.get_instance()

        watcher.assign_watcher('u1', 'alice')
        watcher.assign_watcher('u2', 'bob')
        assert watcher.get_watcher_count() == 2

        watcher.release_all()
        assert watcher.get_watcher_count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Goal Manager Registration Test
# ═══════════════════════════════════════════════════════════════════════════

class TestSelfHealGoalType:

    def test_self_heal_registered(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        assert 'self_heal' in types

    def test_self_heal_prompt_builder(self):
        from integrations.agent_engine.goal_manager import GoalManager

        goal_dict = {
            'goal_type': 'self_heal',
            'title': 'Fix KeyError in create_recipe.get_response',
            'description': 'KeyError occurs 5 times',
            'config': {
                'exc_type': 'KeyError',
                'source_module': 'create_recipe',
                'source_function': 'get_response',
                'occurrence_count': 5,
                'sample_traceback': 'Traceback ...',
            },
        }

        prompt = GoalManager.build_prompt(goal_dict)
        assert 'SELF-HEALING CODE AGENT' in prompt
        assert 'KeyError' in prompt
        assert 'create_recipe' in prompt
        assert 'get_response' in prompt


# ═══════════════════════════════════════════════════════════════════════════
# Bootstrap Goal Seeding Test
# ═══════════════════════════════════════════════════════════════════════════

class TestBootstrapExceptionWatcher:

    def test_bootstrap_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_exception_watcher' in slugs

    def test_bootstrap_goal_config(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = [g for g in SEED_BOOTSTRAP_GOALS if g['slug'] == 'bootstrap_exception_watcher'][0]
        assert goal['goal_type'] == 'self_heal'
        assert goal['config']['mode'] == 'watch'
        assert goal['config']['continuous'] is True
