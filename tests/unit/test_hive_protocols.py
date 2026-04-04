"""Comprehensive tests for hive_signal_bridge.py and hive_task_protocol.py.

Covers:
  - HiveSignalBridge: classification, on_message handler, routing, stats/feed,
    blueprint creation, singleton
  - HiveTaskProtocol: HiveTask dataclass, estimate_complexity, validate_result,
    HiveTaskDispatcher (create, dispatch, cancel, result handling, stats),
    singleton, persistence

Run: pytest tests/unit/test_hive_protocols.py -v --noconftest
"""

import collections
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════
# Helper: mock message objects
# ═══════════════════════════════════════════════════════════════════════

def _make_message(content='', channel='discord', sender_id='user123',
                  sender_name='Alice', msg_id='msg_001', is_group=False):
    """Create a mock message object with the expected attributes."""
    return SimpleNamespace(
        id=msg_id,
        content=content,
        channel=channel,
        sender_id=sender_id,
        sender_name=sender_name,
        is_group=is_group,
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. HiveSignalBridge.classify_signal()
# ═══════════════════════════════════════════════════════════════════════

class TestClassifySignal(unittest.TestCase):
    """Test the fast heuristic signal classifier."""

    def setUp(self):
        from integrations.channels.hive_signal_bridge import HiveSignalBridge
        self.bridge = HiveSignalBridge()

    # ── Single-signal detection ────────────────────────────────────────

    def test_compute_interest_gpu(self):
        """'idle RTX 4090' triggers COMPUTE_INTEREST."""
        signals = self.bridge.classify_signal("I have an idle RTX 4090")
        self.assertIn('COMPUTE_INTEREST', signals)

    def test_bug_report_keywords(self):
        """Bug-related words trigger BUG_REPORT."""
        signals = self.bridge.classify_signal("bug crash error traceback")
        self.assertIn('BUG_REPORT', signals)

    def test_open_source_signal_huggingface(self):
        """'new model release on huggingface' triggers OPEN_SOURCE_SIGNAL."""
        signals = self.bridge.classify_signal(
            "new model release on huggingface"
        )
        self.assertIn('OPEN_SOURCE_SIGNAL', signals)

    def test_support_needed_how_to(self):
        """'how do i configure' triggers SUPPORT_NEEDED."""
        signals = self.bridge.classify_signal("how do i configure this?")
        self.assertIn('SUPPORT_NEEDED', signals)

    def test_recruitment_lead_contribute(self):
        """'contribute to this project' triggers RECRUITMENT_LEAD."""
        signals = self.bridge.classify_signal(
            "I'd love to contribute to this project"
        )
        self.assertIn('RECRUITMENT_LEAD', signals)

    def test_feature_request_dark_mode(self):
        """'could you add a dark mode' triggers FEATURE_REQUEST."""
        signals = self.bridge.classify_signal("could you add a dark mode?")
        self.assertIn('FEATURE_REQUEST', signals)

    def test_sentiment_positive(self):
        """Positive words trigger SENTIMENT."""
        signals = self.bridge.classify_signal("great job, love it!")
        self.assertIn('SENTIMENT', signals)

    def test_sentiment_negative(self):
        """Negative words trigger SENTIMENT."""
        signals = self.bridge.classify_signal("this is terrible and useless")
        self.assertIn('SENTIMENT', signals)

    # ── Multi-signal detection ─────────────────────────────────────────

    def test_multi_signal_gpu_and_bug(self):
        """A message can match multiple signal types."""
        signals = self.bridge.classify_signal("I have a GPU and found a bug")
        self.assertIn('COMPUTE_INTEREST', signals)
        self.assertIn('BUG_REPORT', signals)

    def test_multi_signal_support_and_sentiment(self):
        """Support + positive sentiment in one message."""
        signals = self.bridge.classify_signal(
            "thanks for the help, how do i set up the config?"
        )
        self.assertIn('SUPPORT_NEEDED', signals)
        self.assertIn('SENTIMENT', signals)

    # ── Edge cases ────────────────────────────────────────────────────

    def test_empty_text_no_signals(self):
        """Empty string returns no signals."""
        signals = self.bridge.classify_signal("")
        self.assertEqual(signals, [])

    def test_short_text_no_signals(self):
        """Very short irrelevant text returns no signals."""
        signals = self.bridge.classify_signal("ok")
        self.assertEqual(signals, [])

    def test_unrelated_text_no_signals(self):
        """Unrelated text triggers no keyword-based signals."""
        signals = self.bridge.classify_signal(
            "The weather is pleasant today in Tokyo"
        )
        self.assertEqual(signals, [])

    # ── URL-based signals ──────────────────────────────────────────────

    def test_url_huggingface_co(self):
        """huggingface.co URL triggers OPEN_SOURCE_SIGNAL."""
        signals = self.bridge.classify_signal(
            "Check out huggingface.co/model/new-llama"
        )
        self.assertIn('OPEN_SOURCE_SIGNAL', signals)

    def test_url_arxiv_org(self):
        """arxiv.org URL triggers OPEN_SOURCE_SIGNAL."""
        signals = self.bridge.classify_signal(
            "Interesting paper at arxiv.org/abs/2405.12345"
        )
        self.assertIn('OPEN_SOURCE_SIGNAL', signals)

    # ── Multi-word phrase detection ────────────────────────────────────

    def test_phrase_how_do_i(self):
        """Multi-word phrase 'how do i' triggers SUPPORT_NEEDED."""
        signals = self.bridge.classify_signal(
            "how do i install the dependencies?"
        )
        self.assertIn('SUPPORT_NEEDED', signals)

    def test_phrase_would_be_nice(self):
        """Multi-word phrase 'would be nice' triggers FEATURE_REQUEST."""
        signals = self.bridge.classify_signal(
            "It would be nice to have dark mode"
        )
        self.assertIn('FEATURE_REQUEST', signals)

    def test_phrase_open_source(self):
        """Multi-word phrase 'open source' triggers RECRUITMENT_LEAD."""
        signals = self.bridge.classify_signal(
            "I believe in open source and want to help"
        )
        self.assertIn('RECRUITMENT_LEAD', signals)


# ═══════════════════════════════════════════════════════════════════════
# 2. HiveSignalBridge._on_message()
# ═══════════════════════════════════════════════════════════════════════

class TestOnMessage(unittest.TestCase):
    """Test the core _on_message handler."""

    def setUp(self):
        from integrations.channels.hive_signal_bridge import HiveSignalBridge
        self.bridge = HiveSignalBridge()

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._get_executor')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    def test_on_message_updates_signal_counts(self, mock_spark, mock_event,
                                               mock_executor):
        """_on_message increments signal_counts for matched signals."""
        mock_executor.return_value = MagicMock()
        msg = _make_message(content="I have an idle GPU", channel='telegram')
        self.bridge._on_message(msg)

        stats = self.bridge.get_stats()
        self.assertGreater(stats['by_type']['COMPUTE_INTEREST'], 0)

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._get_executor')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    def test_on_message_updates_channel_counts(self, mock_spark, mock_event,
                                                mock_executor):
        """_on_message increments channel_counts."""
        mock_executor.return_value = MagicMock()
        msg = _make_message(content="bug found in login flow", channel='slack')
        self.bridge._on_message(msg)

        stats = self.bridge.get_stats()
        self.assertIn('slack', stats['by_channel'])
        self.assertEqual(stats['by_channel']['slack'], 1)

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._get_executor')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    def test_on_message_adds_to_feed(self, mock_spark, mock_event,
                                      mock_executor):
        """_on_message appends entry to signal_feed."""
        mock_executor.return_value = MagicMock()
        msg = _make_message(content="crash on startup", channel='discord')
        self.bridge._on_message(msg)

        feed = self.bridge.get_signal_feed()
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]['channel'], 'discord')
        self.assertIn('BUG_REPORT', feed[0]['signals'])

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._get_executor')
    def test_on_message_never_raises(self, mock_executor):
        """_on_message must never raise -- it would break the channel adapter."""
        mock_executor.side_effect = RuntimeError("executor broken")
        msg = _make_message(content="crash error bug")
        # Should not raise
        self.bridge._on_message(msg)

    def test_on_message_empty_content_noop(self):
        """Empty message content results in no stats update."""
        msg = _make_message(content="")
        self.bridge._on_message(msg)
        stats = self.bridge.get_stats()
        self.assertEqual(stats['total_messages'], 0)

    def test_on_message_short_content_noop(self):
        """Very short content (< 2 chars) is ignored."""
        msg = _make_message(content="x")
        self.bridge._on_message(msg)
        stats = self.bridge.get_stats()
        self.assertEqual(stats['total_messages'], 0)

    def test_on_message_no_matching_signals_noop(self):
        """Message with no matching signals does not update stats."""
        msg = _make_message(content="just a random sentence about nothing")
        self.bridge._on_message(msg)
        stats = self.bridge.get_stats()
        self.assertEqual(stats['total_messages'], 0)


# ═══════════════════════════════════════════════════════════════════════
# 3. HiveSignalBridge routing (mocked dependencies)
# ═══════════════════════════════════════════════════════════════════════

class TestSignalRouting(unittest.TestCase):
    """Test that signals route to the correct hive subsystems."""

    def setUp(self):
        from integrations.channels.hive_signal_bridge import HiveSignalBridge
        self.bridge = HiveSignalBridge()

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.agent_engine.dispatch.dispatch_goal')
    def test_compute_interest_routes_to_dispatch_goal(self, mock_dispatch,
                                                       mock_spark, mock_event):
        """COMPUTE_INTEREST routes to dispatch_goal with hive_growth type."""
        msg = _make_message(content="I have an idle GPU ready")
        self.bridge._route_compute_interest(msg, ['COMPUTE_INTEREST'])

        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args
        self.assertEqual(call_kwargs[1]['goal_type'], 'hive_growth')
        self.assertIn('Compute recruitment', call_kwargs[1]['prompt'])

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.coding_agent.hive_task_protocol.get_dispatcher')
    def test_bug_report_routes_to_hive_task_dispatcher(self, mock_get_disp,
                                                        mock_spark, mock_event):
        """BUG_REPORT routes to HiveTaskDispatcher.create_task."""
        mock_dispatcher = MagicMock()
        mock_get_disp.return_value = mock_dispatcher

        msg = _make_message(content="crash in the login page", channel='matrix')
        self.bridge._route_bug_report(msg, ['BUG_REPORT'])

        mock_dispatcher.create_task.assert_called_once()
        call_kwargs = mock_dispatcher.create_task.call_args[1]
        self.assertEqual(call_kwargs['task_type'], 'bug_fix')
        self.assertIn('Bug report from matrix', call_kwargs['title'])

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.coding_agent.hive_task_protocol.get_dispatcher',
           side_effect=ImportError("not available"))
    @patch('integrations.agent_engine.instruction_queue.enqueue_instruction')
    def test_bug_report_falls_back_to_instruction_queue(
            self, mock_enqueue, mock_get_disp, mock_spark, mock_event):
        """BUG_REPORT falls back to instruction_queue if dispatcher unavailable."""
        msg = _make_message(content="error on submit button")
        self.bridge._route_bug_report(msg, ['BUG_REPORT'])

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args[1]
        self.assertEqual(call_kwargs['priority'], 7)
        self.assertIn('bug', call_kwargs['tags'])

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.agent_engine.dispatch.dispatch_goal')
    def test_support_needed_routes_to_dispatch_goal(self, mock_dispatch,
                                                     mock_spark, mock_event):
        """SUPPORT_NEEDED routes to dispatch_goal with 'support' type."""
        msg = _make_message(content="how do i set up this thing?",
                            sender_id='user456')
        self.bridge._route_support_needed(msg, ['SUPPORT_NEEDED'])

        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        self.assertEqual(call_kwargs['goal_type'], 'support')
        self.assertEqual(call_kwargs['user_id'], 'user456')

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.agent_engine.dispatch.dispatch_goal')
    def test_recruitment_lead_routes_to_dispatch_goal(self, mock_dispatch,
                                                       mock_spark, mock_event):
        """RECRUITMENT_LEAD routes to dispatch_goal with high priority prompt."""
        msg = _make_message(content="I want to contribute", sender_name='Bob')
        self.bridge._route_recruitment_lead(msg, ['RECRUITMENT_LEAD'])

        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        self.assertEqual(call_kwargs['goal_type'], 'hive_growth')
        self.assertIn('HIGH PRIORITY', call_kwargs['prompt'])
        self.assertIn('Bob', call_kwargs['prompt'])

    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_signal_event')
    @patch('integrations.channels.hive_signal_bridge.HiveSignalBridge._emit_spark_event')
    @patch('integrations.agent_engine.instruction_queue.enqueue_instruction')
    def test_feature_request_routes_to_instruction_queue(self, mock_enqueue,
                                                          mock_spark,
                                                          mock_event):
        """FEATURE_REQUEST routes to instruction_queue."""
        msg = _make_message(content="could you add dark mode?")
        self.bridge._route_feature_request(msg, ['FEATURE_REQUEST'])

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args[1]
        self.assertEqual(call_kwargs['priority'], 4)
        self.assertIn('feature_request', call_kwargs['tags'])


# ═══════════════════════════════════════════════════════════════════════
# 4. Stats & Feed
# ═══════════════════════════════════════════════════════════════════════

class TestStatsAndFeed(unittest.TestCase):
    """Test get_stats() and get_signal_feed()."""

    def setUp(self):
        from integrations.channels.hive_signal_bridge import HiveSignalBridge
        self.bridge = HiveSignalBridge()

    def test_get_stats_structure(self):
        """get_stats() returns the expected keys."""
        stats = self.bridge.get_stats()
        self.assertIn('by_type', stats)
        self.assertIn('by_channel', stats)
        self.assertIn('total_messages', stats)
        self.assertIn('attached_adapters', stats)
        self.assertEqual(stats['total_messages'], 0)
        self.assertIsInstance(stats['by_type'], dict)

    def test_get_stats_all_signal_types_present(self):
        """get_stats()['by_type'] has all signal type keys initialized to 0."""
        from integrations.channels.hive_signal_bridge import ALL_SIGNAL_TYPES
        stats = self.bridge.get_stats()
        for st in ALL_SIGNAL_TYPES:
            self.assertIn(st, stats['by_type'])
            self.assertEqual(stats['by_type'][st], 0)

    def test_get_signal_feed_empty(self):
        """Fresh bridge returns empty feed."""
        feed = self.bridge.get_signal_feed()
        self.assertEqual(feed, [])

    def test_get_signal_feed_respects_limit(self):
        """Feed returns at most `limit` entries."""
        # Manually inject entries
        for i in range(10):
            self.bridge._signal_feed.append({
                'message_id': f'msg_{i}',
                'signals': ['BUG_REPORT'],
                'timestamp': time.time() + i,
            })
        feed = self.bridge.get_signal_feed(limit=3)
        self.assertEqual(len(feed), 3)

    def test_get_signal_feed_most_recent_first(self):
        """Feed entries are ordered most-recent first."""
        for i in range(5):
            self.bridge._signal_feed.append({
                'message_id': f'msg_{i}',
                'signals': ['BUG_REPORT'],
                'timestamp': float(i),
            })
        feed = self.bridge.get_signal_feed(limit=5)
        self.assertEqual(feed[0]['message_id'], 'msg_4')
        self.assertEqual(feed[-1]['message_id'], 'msg_0')


# ═══════════════════════════════════════════════════════════════════════
# 5. Blueprint
# ═══════════════════════════════════════════════════════════════════════

class TestSignalBlueprint(unittest.TestCase):
    """Test create_signal_blueprint()."""

    def test_blueprint_returns_flask_blueprint(self):
        """create_signal_blueprint() returns a Flask Blueprint with 3 routes."""
        from integrations.channels.hive_signal_bridge import (
            create_signal_blueprint,
        )
        bp = create_signal_blueprint()
        if bp is None:
            self.skipTest("Flask not installed")
        from flask import Blueprint
        self.assertIsInstance(bp, Blueprint)
        self.assertEqual(bp.name, 'hive_signals')

        # Blueprint should have registered 3 deferred view functions
        # (stats, feed, classify)
        self.assertGreaterEqual(len(bp.deferred_functions), 3)


# ═══════════════════════════════════════════════════════════════════════
# 6. Singleton
# ═══════════════════════════════════════════════════════════════════════

class TestSignalBridgeSingleton(unittest.TestCase):
    """Test get_signal_bridge() singleton behavior."""

    def test_singleton_returns_same_instance(self):
        import integrations.channels.hive_signal_bridge as mod
        # Reset singleton for test isolation
        mod._bridge = None
        a = mod.get_signal_bridge()
        b = mod.get_signal_bridge()
        self.assertIs(a, b)
        # Clean up
        mod._bridge = None


# ═══════════════════════════════════════════════════════════════════════
# 7. HiveTask dataclass
# ═══════════════════════════════════════════════════════════════════════

class TestHiveTask(unittest.TestCase):
    """Test HiveTask creation, serialization, and deserialization."""

    def test_creation(self):
        from integrations.coding_agent.hive_task_protocol import HiveTask
        task = HiveTask(
            task_id='abc-123',
            task_type='bug_fix',
            title='Fix login',
            description='Login page crashes on submit',
            instructions='Investigate login.py and fix the exception.',
        )
        self.assertEqual(task.task_id, 'abc-123')
        self.assertEqual(task.task_type, 'bug_fix')
        self.assertEqual(task.status, 'pending')
        self.assertTrue(task.requires_tests)

    def test_to_dict(self):
        from integrations.coding_agent.hive_task_protocol import HiveTask
        task = HiveTask(
            task_id='abc-123',
            task_type='code_write',
            title='Add dark mode',
            description='Implement dark mode theme',
            instructions='Create theme toggle in settings.',
        )
        d = task.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d['task_id'], 'abc-123')
        self.assertEqual(d['task_type'], 'code_write')

    def test_from_dict_round_trip(self):
        """to_dict -> from_dict produces equivalent task."""
        from integrations.coding_agent.hive_task_protocol import HiveTask
        original = HiveTask(
            task_id='xyz-789',
            task_type='refactor',
            title='Refactor auth module',
            description='Split auth.py into smaller files',
            instructions='Extract helpers into auth_helpers.py',
            priority=80,
            spark_reward=42,
            files_scope=['auth.py', 'auth_helpers.py'],
        )
        d = original.to_dict()
        restored = HiveTask.from_dict(d)
        self.assertEqual(restored.task_id, original.task_id)
        self.assertEqual(restored.priority, 80)
        self.assertEqual(restored.spark_reward, 42)
        self.assertEqual(restored.files_scope, ['auth.py', 'auth_helpers.py'])

    def test_from_dict_tolerates_missing_keys(self):
        """from_dict works with minimal dict (only required fields)."""
        from integrations.coding_agent.hive_task_protocol import HiveTask
        d = {
            'task_id': 'minimal-1',
            'task_type': 'bug_fix',
            'title': 'Test',
            'description': 'Desc',
            'instructions': 'Fix it',
        }
        task = HiveTask.from_dict(d)
        self.assertEqual(task.task_id, 'minimal-1')
        self.assertEqual(task.status, 'pending')  # default

    def test_from_dict_ignores_unknown_keys(self):
        """from_dict silently ignores keys not in the dataclass."""
        from integrations.coding_agent.hive_task_protocol import HiveTask
        d = {
            'task_id': 'extra-1',
            'task_type': 'code_test',
            'title': 'T',
            'description': 'D',
            'instructions': 'I',
            'nonexistent_field': 'should be ignored',
            'another_unknown': 999,
        }
        task = HiveTask.from_dict(d)
        self.assertEqual(task.task_id, 'extra-1')
        self.assertFalse(hasattr(task, 'nonexistent_field'))


# ═══════════════════════════════════════════════════════════════════════
# 8. estimate_complexity()
# ═══════════════════════════════════════════════════════════════════════

class TestEstimateComplexity(unittest.TestCase):
    """Test the heuristic complexity estimator."""

    def test_short_text_low_score(self):
        """Short, simple instructions yield a low score."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        score = estimate_complexity("Fix the typo in README")
        self.assertGreaterEqual(score, 1)
        self.assertLessEqual(score, 15)

    def test_long_text_with_files_high_score(self):
        """Long instructions with file references yield higher score."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        instructions = (
            "Refactor the authentication module across multiple files. "
            "Modify auth.py, auth_helpers.py, login_view.py, "
            "test_auth.py, models.py, and security_middleware.py. "
            + "x" * 600  # Pad to increase length score
        )
        score = estimate_complexity(instructions)
        self.assertGreater(score, 25)

    def test_security_keywords_boost(self):
        """Security keywords add points."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        base = estimate_complexity("Fix the login page")
        boosted = estimate_complexity("Fix the security vulnerability in login")
        self.assertGreater(boosted, base)

    def test_test_keywords_boost(self):
        """Testing keywords add points."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        base = estimate_complexity("Update the user form")
        boosted = estimate_complexity("Update the user form and add pytest coverage")
        self.assertGreater(boosted, base)

    def test_refactor_keywords_boost(self):
        """Refactoring keywords add points."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        base = estimate_complexity("Change the config loader")
        boosted = estimate_complexity("Refactor and restructure the config loader")
        self.assertGreater(boosted, base)

    def test_score_clamped_1_to_100(self):
        """Score is always between 1 and 100."""
        from integrations.coding_agent.hive_task_protocol import (
            estimate_complexity,
        )
        self.assertGreaterEqual(estimate_complexity(""), 1)
        massive = "refactor migrate rewrite security vulnerability " * 200
        massive += " ".join(f"file{i}.py" for i in range(50))
        self.assertLessEqual(estimate_complexity(massive), 100)


# ═══════════════════════════════════════════════════════════════════════
# 9. validate_result()
# ═══════════════════════════════════════════════════════════════════════

class TestValidateResult(unittest.TestCase):
    """Test the task result quality scorer."""

    def _make_task(self, **overrides):
        from integrations.coding_agent.hive_task_protocol import HiveTask
        defaults = dict(
            task_id='test-task',
            task_type='bug_fix',
            title='Fix bug',
            description='Fix the thing',
            instructions='Do the fix',
            files_scope=['main.py'],
            requires_tests=True,
        )
        defaults.update(overrides)
        return HiveTask(**defaults)

    @patch('security.dlp_engine.get_dlp_engine',
           side_effect=ImportError("no DLP"), create=True)
    def test_good_result_high_score(self, _):
        """Result with files_changed, tests_passed, no error scores high."""
        from integrations.coding_agent.hive_task_protocol import validate_result
        task = self._make_task()
        result = {
            'files_changed': ['main.py'],
            'tests_passed': True,
        }
        score = validate_result(task, result)
        self.assertGreaterEqual(score, 0.7)

    @patch('security.dlp_engine.get_dlp_engine',
           side_effect=ImportError("no DLP"), create=True)
    def test_missing_files_partial_score(self, _):
        """Result missing files_changed gets partial score."""
        from integrations.coding_agent.hive_task_protocol import validate_result
        task = self._make_task()
        result = {
            'tests_passed': True,
        }
        score = validate_result(task, result)
        # No files_changed means first check fails
        self.assertLess(score, 0.9)

    @patch('security.dlp_engine.get_dlp_engine',
           side_effect=ImportError("no DLP"), create=True)
    def test_error_present_lower_score(self, _):
        """Result with an error field gets a lower score."""
        from integrations.coding_agent.hive_task_protocol import validate_result
        task = self._make_task()
        good_result = {
            'files_changed': ['main.py'],
            'tests_passed': True,
        }
        error_result = {
            'files_changed': ['main.py'],
            'tests_passed': True,
            'error': 'RuntimeError: something went wrong',
        }
        good_score = validate_result(task, good_result)
        error_score = validate_result(task, error_result)
        self.assertGreater(good_score, error_score)

    @patch('security.dlp_engine.get_dlp_engine',
           side_effect=ImportError("no DLP"), create=True)
    def test_empty_result_low_score(self, _):
        """Empty result dict gets a low score."""
        from integrations.coding_agent.hive_task_protocol import validate_result
        task = self._make_task()
        score = validate_result(task, {})
        self.assertLess(score, 0.5)

    @patch('security.dlp_engine.get_dlp_engine',
           side_effect=ImportError("no DLP"), create=True)
    def test_no_tests_required_higher_score(self, _):
        """When requires_tests=False, missing tests don't penalize."""
        from integrations.coding_agent.hive_task_protocol import validate_result
        task_req = self._make_task(requires_tests=True)
        task_noreq = self._make_task(requires_tests=False)
        result = {'files_changed': ['main.py']}
        score_req = validate_result(task_req, result)
        score_noreq = validate_result(task_noreq, result)
        # Without test requirement, score should be at least as high
        self.assertGreaterEqual(score_noreq, score_req)


# ═══════════════════════════════════════════════════════════════════════
# 10. HiveTaskDispatcher
# ═══════════════════════════════════════════════════════════════════════

class TestHiveTaskDispatcher(unittest.TestCase):
    """Test the dispatcher: create, query, cancel, result handling."""

    def _make_dispatcher(self):
        """Create a dispatcher with a temp file for persistence."""
        import integrations.coding_agent.hive_task_protocol as mod
        # Point persistence to a temp file
        self._tmpdir = tempfile.mkdtemp()
        self._tasks_file = os.path.join(self._tmpdir, 'hive_tasks.json')
        self._orig_tasks_file = mod._TASKS_FILE
        mod._TASKS_FILE = self._tasks_file

        dispatcher = mod.HiveTaskDispatcher()
        return dispatcher

    def tearDown(self):
        import integrations.coding_agent.hive_task_protocol as mod
        # Restore original path
        if hasattr(self, '_orig_tasks_file'):
            mod._TASKS_FILE = self._orig_tasks_file
        # Clean up temp dir
        if hasattr(self, '_tmpdir'):
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_task_generates_uuid(self):
        """create_task() generates a UUID task_id."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task(
            task_type='bug_fix',
            title='Fix login',
            description='Login is broken',
            instructions='Check auth.py for the exception',
        )
        self.assertTrue(len(task.task_id) > 10)
        # UUID format: 8-4-4-4-12
        parts = task.task_id.split('-')
        self.assertEqual(len(parts), 5)

    def test_create_task_auto_calculates_spark(self):
        """create_task() auto-calculates spark_reward from complexity."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task(
            task_type='bug_fix',
            title='Fix security vulnerability',
            description='CVE found',
            instructions='Fix the security vulnerability in auth module. '
                         'Modify auth.py and add pytest tests.',
        )
        # Should have auto-calculated, not default 10
        self.assertGreater(task.spark_reward, 0)

    def test_create_task_explicit_spark(self):
        """create_task() respects explicitly provided spark_reward."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task(
            task_type='code_write',
            title='Feature',
            description='New feature',
            instructions='Build it',
            spark_reward=77,
        )
        self.assertEqual(task.spark_reward, 77)

    def test_get_pending_tasks_sorted_by_priority(self):
        """get_pending_tasks() returns tasks sorted by priority (highest first)."""
        dispatcher = self._make_dispatcher()
        dispatcher.create_task('code_write', 'Low', 'D', 'I', priority=10)
        dispatcher.create_task('bug_fix', 'High', 'D', 'I', priority=90)
        dispatcher.create_task('code_test', 'Mid', 'D', 'I', priority=50)

        pending = dispatcher.get_pending_tasks()
        self.assertEqual(len(pending), 3)
        priorities = [t.priority for t in pending]
        self.assertEqual(priorities, [90, 50, 10])

    def test_cancel_task_pending(self):
        """cancel_task() successfully cancels a pending task."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task('code_write', 'T', 'D', 'I')
        self.assertTrue(dispatcher.cancel_task(task.task_id))
        self.assertEqual(
            dispatcher.get_task(task.task_id).status, 'cancelled'
        )

    def test_cancel_task_rejects_completed(self):
        """cancel_task() returns False for completed/validated tasks."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task('code_write', 'T', 'D', 'I')
        # Manually mark as validated
        with dispatcher._lock:
            task.status = 'validated'
        self.assertFalse(dispatcher.cancel_task(task.task_id))

    def test_cancel_task_unknown_id(self):
        """cancel_task() returns False for non-existent task_id."""
        dispatcher = self._make_dispatcher()
        self.assertFalse(dispatcher.cancel_task('nonexistent-id'))

    @patch('integrations.coding_agent.hive_task_protocol.validate_result',
           return_value=0.8)
    def test_on_task_result_validated(self, mock_validate):
        """on_task_result() with quality >= 0.4 sets VALIDATED + awards Spark."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task(
            'bug_fix', 'Fix', 'D', 'I', spark_reward=50,
        )
        result_data = {
            'files_changed': ['main.py'],
            'tests_passed': True,
        }
        outcome = dispatcher.on_task_result(task.task_id, result_data)

        self.assertTrue(outcome['validated'])
        self.assertGreater(outcome['spark_awarded'], 0)
        self.assertAlmostEqual(outcome['quality_score'], 0.8)
        self.assertEqual(
            dispatcher.get_task(task.task_id).status, 'validated'
        )

    @patch('integrations.coding_agent.hive_task_protocol.validate_result',
           return_value=0.2)
    def test_on_task_result_failed(self, mock_validate):
        """on_task_result() with quality < 0.4 sets FAILED, no Spark."""
        dispatcher = self._make_dispatcher()
        task = dispatcher.create_task(
            'code_write', 'Feature', 'D', 'I', spark_reward=30,
        )
        outcome = dispatcher.on_task_result(task.task_id, {'error': 'oops'})

        self.assertFalse(outcome['validated'])
        self.assertEqual(outcome['spark_awarded'], 0)
        self.assertEqual(
            dispatcher.get_task(task.task_id).status, 'failed'
        )

    def test_on_task_result_unknown_task(self):
        """on_task_result() for unknown task_id returns error dict."""
        dispatcher = self._make_dispatcher()
        outcome = dispatcher.on_task_result('no-such-task', {})
        self.assertFalse(outcome['validated'])
        self.assertEqual(outcome.get('error'), 'unknown_task')

    def test_get_stats_counts(self):
        """get_stats() returns correct created/pending counts."""
        dispatcher = self._make_dispatcher()
        dispatcher.create_task('code_write', 'A', 'D', 'I')
        dispatcher.create_task('bug_fix', 'B', 'D', 'I')

        stats = dispatcher.get_stats()
        self.assertEqual(stats['total_created'], 2)
        self.assertEqual(stats['pending_count'], 2)
        self.assertEqual(stats['total_completed'], 0)
        self.assertEqual(stats['total_failed'], 0)
        self.assertIn('avg_quality', stats)
        self.assertIn('active_count', stats)
        self.assertIn('total_tasks', stats)

    @patch('integrations.coding_agent.hive_task_protocol.HiveTaskDispatcher.match_session',
           return_value=None)
    def test_dispatch_pending_no_sessions(self, mock_match):
        """dispatch_pending() returns 0 when no sessions available."""
        dispatcher = self._make_dispatcher()
        dispatcher.create_task('code_write', 'T', 'D', 'I')
        dispatched = dispatcher.dispatch_pending()
        self.assertEqual(dispatched, 0)


# ═══════════════════════════════════════════════════════════════════════
# 11. Dispatcher singleton
# ═══════════════════════════════════════════════════════════════════════

class TestDispatcherSingleton(unittest.TestCase):
    """Test get_dispatcher() singleton behavior."""

    def test_singleton_returns_same_instance(self):
        import integrations.coding_agent.hive_task_protocol as mod
        # Reset singleton for test isolation
        old = mod._dispatcher
        mod._dispatcher = None
        try:
            a = mod.get_dispatcher()
            b = mod.get_dispatcher()
            self.assertIs(a, b)
        finally:
            mod._dispatcher = old


# ═══════════════════════════════════════════════════════════════════════
# 12. Persistence: tasks survive save/load cycle
# ═══════════════════════════════════════════════════════════════════════

class TestPersistence(unittest.TestCase):
    """Test that tasks persist to disk and reload correctly."""

    def test_tasks_survive_save_load_cycle(self):
        """Tasks created by one dispatcher instance are loaded by another."""
        import integrations.coding_agent.hive_task_protocol as mod

        tmpdir = tempfile.mkdtemp()
        tasks_file = os.path.join(tmpdir, 'hive_tasks.json')
        orig = mod._TASKS_FILE
        mod._TASKS_FILE = tasks_file

        try:
            # First dispatcher: create tasks
            d1 = mod.HiveTaskDispatcher()
            t1 = d1.create_task('bug_fix', 'Persist Test', 'Desc', 'Instr',
                                priority=75)
            t2 = d1.create_task('code_write', 'Second', 'D2', 'I2',
                                spark_reward=33)
            task_id_1 = t1.task_id
            task_id_2 = t2.task_id

            # Second dispatcher: loads from same file
            d2 = mod.HiveTaskDispatcher()
            loaded_1 = d2.get_task(task_id_1)
            loaded_2 = d2.get_task(task_id_2)

            self.assertIsNotNone(loaded_1)
            self.assertIsNotNone(loaded_2)
            self.assertEqual(loaded_1.title, 'Persist Test')
            self.assertEqual(loaded_1.priority, 75)
            self.assertEqual(loaded_2.spark_reward, 33)
        finally:
            mod._TASKS_FILE = orig
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_empty_file(self):
        """Dispatcher handles empty/missing tasks file gracefully."""
        import integrations.coding_agent.hive_task_protocol as mod

        tmpdir = tempfile.mkdtemp()
        tasks_file = os.path.join(tmpdir, 'hive_tasks.json')
        orig = mod._TASKS_FILE
        mod._TASKS_FILE = tasks_file

        try:
            d = mod.HiveTaskDispatcher()
            self.assertEqual(len(d.get_pending_tasks()), 0)
        finally:
            mod._TASKS_FILE = orig
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_corrupt_file(self):
        """Dispatcher handles corrupt JSON gracefully."""
        import integrations.coding_agent.hive_task_protocol as mod

        tmpdir = tempfile.mkdtemp()
        tasks_file = os.path.join(tmpdir, 'hive_tasks.json')
        orig = mod._TASKS_FILE
        mod._TASKS_FILE = tasks_file

        try:
            os.makedirs(os.path.dirname(tasks_file), exist_ok=True)
            with open(tasks_file, 'w') as f:
                f.write("{corrupted json!!!}")
            d = mod.HiveTaskDispatcher()
            self.assertEqual(len(d.get_pending_tasks()), 0)
        finally:
            mod._TASKS_FILE = orig
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
