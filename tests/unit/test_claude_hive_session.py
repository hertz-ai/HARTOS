"""
Tests for integrations/coding_agent/claude_hive_session.py

Covers:
  - ClaudeHiveSession lifecycle (connect, disconnect, pause, resume)
  - Task reception, queue limits, scope checking
  - Result reporting with stats updates (spark, quality, tasks_completed)
  - get_status() / get_tasks() / set_task_scope()
  - SessionRegistry (register, unregister, get_session, get_available_sessions)
  - Thread safety for concurrent register/unregister
  - Module singletons (get_hive_session, get_session_registry)
  - Flask Blueprint endpoints
  - Constants sanity checks

Run: pytest tests/unit/test_claude_hive_session.py -v --noconftest
"""

import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

import integrations.coding_agent.claude_hive_session as mod
from integrations.coding_agent.claude_hive_session import (
    ClaudeHiveSession,
    SessionRegistry,
    STATUS_DISCONNECTED,
    STATUS_CONNECTING,
    STATUS_IDLE,
    STATUS_WORKING,
    STATUS_PAUSED,
    EVENT_TASK_DISPATCHED,
    EVENT_TASK_COMPLETED,
    EVENT_SESSION_CONNECTED,
    EVENT_SESSION_DISCONNECTED,
    SPARK_BASE_REWARD,
    SPARK_COMPLEXITY_MULTIPLIER,
    SPARK_QUALITY_BONUS_THRESHOLD,
    MAX_PENDING_TASKS,
    TASK_SCOPES,
    PEER_TYPE,
    SESSION_CAPABILITIES_DEFAULT,
    create_hive_session_blueprint,
)


def _make_valid_task(task_id='task_001', trust_level='same_user',
                     scope_level='own_repos', **overrides):
    """Build a minimal task dict that passes all validations."""
    t = {
        'task_id': task_id,
        'description': 'Fix the bug in helper.py',
        'trust_level': trust_level,
        'scope_level': scope_level,
        'target_files': ['helper.py'],
        'origin_signature': '',
        'priority': 5,
    }
    t.update(overrides)
    return t


# All heavy imports (PeerLink, EventBus, shard engine, instruction queue,
# dispatch, master_key) are mocked so tests run without those subsystems.

_PATCH_PREFIX = 'integrations.coding_agent.claude_hive_session'


@patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
@patch(f'{_PATCH_PREFIX}.emit_event', create=True)
class TestClaudeHiveSessionConnect(unittest.TestCase):
    """Tests for ClaudeHiveSession.connect()."""

    def _make_session(self):
        return ClaudeHiveSession()

    # -- connect() happy path -------------------------------------------

    def test_connect_sets_status_idle_and_returns_session_id(
            self, mock_emit, mock_peer):
        session = self._make_session()
        result = session.connect(user_id='u1')

        self.assertTrue(result['success'])
        self.assertEqual(result['status'], STATUS_IDLE)
        self.assertTrue(result['session_id'].startswith('chs_'))
        self.assertEqual(session.status, STATUS_IDLE)
        self.assertEqual(session.user_id, 'u1')

    def test_connect_uses_default_capabilities(self, mock_emit, mock_peer):
        session = self._make_session()
        result = session.connect(user_id='u1')

        self.assertEqual(result['capabilities'], dict(SESSION_CAPABILITIES_DEFAULT))

    def test_connect_accepts_custom_capabilities(self, mock_emit, mock_peer):
        session = self._make_session()
        custom = {'languages': ['rust'], 'can_run_tests': False}
        result = session.connect(user_id='u1', capabilities=custom)

        self.assertEqual(result['capabilities'], custom)

    def test_connect_records_connected_since(self, mock_emit, mock_peer):
        session = self._make_session()
        before = time.time()
        session.connect(user_id='u1')
        after = time.time()

        self.assertIsNotNone(session.stats['connected_since'])
        self.assertGreaterEqual(session.stats['connected_since'], before)
        self.assertLessEqual(session.stats['connected_since'], after)

    # -- connect() with invalid task_scope ------------------------------

    def test_connect_rejects_invalid_task_scope(self, mock_emit, mock_peer):
        session = self._make_session()
        result = session.connect(user_id='u1', task_scope='EVERYTHING')

        self.assertFalse(result['success'])
        self.assertIn('Invalid task_scope', result['error'])
        self.assertEqual(session.status, STATUS_DISCONNECTED)

    # -- connect() when already connected -------------------------------

    def test_connect_rejects_if_already_connected(self, mock_emit, mock_peer):
        session = self._make_session()
        session.connect(user_id='u1')
        result = session.connect(user_id='u2')

        self.assertFalse(result['success'])
        self.assertIn('Already connected', result['error'])

    def test_connect_sets_task_scope(self, mock_emit, mock_peer):
        session = self._make_session()
        result = session.connect(user_id='u1', task_scope='public')

        self.assertEqual(result['task_scope'], 'public')
        self.assertEqual(session.task_scope, 'public')


@patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
@patch(f'{_PATCH_PREFIX}.emit_event', create=True)
class TestClaudeHiveSessionDisconnect(unittest.TestCase):
    """Tests for ClaudeHiveSession.disconnect()."""

    def _connected_session(self, user_id='u1'):
        s = ClaudeHiveSession()
        s.connect(user_id=user_id)
        return s

    def test_disconnect_returns_stats_and_success(self, mock_emit, mock_peer):
        session = self._connected_session()
        sid = session.session_id
        result = session.disconnect()

        self.assertTrue(result['success'])
        self.assertEqual(result['session_id'], sid)
        self.assertIn('stats', result)
        self.assertEqual(session.status, STATUS_DISCONNECTED)

    def test_disconnect_clears_session_id(self, mock_emit, mock_peer):
        session = self._connected_session()
        session.disconnect()

        self.assertEqual(session.session_id, '')
        self.assertIsNone(session._peer_link_id)

    def test_disconnect_on_already_disconnected(self, mock_emit, mock_peer):
        session = ClaudeHiveSession()
        result = session.disconnect()

        self.assertTrue(result['success'])
        self.assertIn('Already disconnected', result.get('message', ''))

    def test_disconnect_includes_flushed_tasks_count(self, mock_emit, mock_peer):
        session = self._connected_session()
        # Manually inject pending tasks
        with session._lock:
            session._pending_tasks = [
                {'task_id': 't1'}, {'task_id': 't2'}
            ]
        result = session.disconnect()

        self.assertEqual(result['flushed_tasks'], 2)


@patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
@patch(f'{_PATCH_PREFIX}.emit_event', create=True)
class TestClaudeHiveSessionReceiveTask(unittest.TestCase):
    """Tests for ClaudeHiveSession.receive_task()."""

    def _connected_session(self, scope='any'):
        s = ClaudeHiveSession()
        s.connect(user_id='u1', task_scope=scope)
        return s

    @patch(f'{_PATCH_PREFIX}.ClaudeHiveSession._execute_next_task')
    def test_receive_task_accepts_valid_task(
            self, mock_exec, mock_emit, mock_peer):
        session = self._connected_session()
        task = _make_valid_task()
        accepted = session.receive_task(task)

        self.assertTrue(accepted)
        self.assertEqual(len(session._pending_tasks), 1)

    def test_receive_task_rejects_when_disconnected(
            self, mock_emit, mock_peer):
        session = ClaudeHiveSession()
        task = _make_valid_task()
        accepted = session.receive_task(task)

        self.assertFalse(accepted)

    @patch(f'{_PATCH_PREFIX}.ClaudeHiveSession._execute_next_task')
    def test_receive_task_rejects_when_queue_full(
            self, mock_exec, mock_emit, mock_peer):
        session = self._connected_session()

        # Fill the queue to MAX_PENDING_TASKS
        with session._lock:
            session._pending_tasks = [
                {'task_id': f't{i}'} for i in range(MAX_PENDING_TASKS)
            ]

        task = _make_valid_task(task_id='overflow_task')
        accepted = session.receive_task(task)
        self.assertFalse(accepted)

    @patch(f'{_PATCH_PREFIX}.ClaudeHiveSession._execute_next_task')
    def test_receive_task_rejects_when_paused(
            self, mock_exec, mock_emit, mock_peer):
        session = self._connected_session()
        session.pause()

        task = _make_valid_task()
        accepted = session.receive_task(task)
        self.assertFalse(accepted)

    @patch(f'{_PATCH_PREFIX}.ClaudeHiveSession._execute_next_task')
    def test_receive_task_rejects_no_signature_and_not_same_user(
            self, mock_exec, mock_emit, mock_peer):
        session = self._connected_session()
        task = _make_valid_task(trust_level='peer', origin_signature='')
        accepted = session.receive_task(task)
        self.assertFalse(accepted)

    @patch(f'{_PATCH_PREFIX}.ClaudeHiveSession._execute_next_task')
    def test_receive_task_scope_mismatch_rejected(
            self, mock_exec, mock_emit, mock_peer):
        session = self._connected_session(scope='own_repos')
        task = _make_valid_task(scope_level='any', repo_owner='someone_else')
        accepted = session.receive_task(task)
        self.assertFalse(accepted)


@patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
@patch(f'{_PATCH_PREFIX}.emit_event', create=True)
class TestClaudeHiveSessionReportResult(unittest.TestCase):
    """Tests for ClaudeHiveSession.report_result()."""

    def _connected_session(self):
        s = ClaudeHiveSession()
        s.connect(user_id='u1')
        return s

    def test_report_result_completed_updates_stats(
            self, mock_emit, mock_peer):
        session = self._connected_session()
        result = {
            'status': 'completed',
            'changes': [{'file': 'a.py', 'diff': '+line'}],
            'test_results': '3 passed',
            'duration_s': 5.0,
            'complexity_score': 3,
        }
        session.report_result('t1', result)

        self.assertEqual(session.stats['tasks_completed'], 1)
        self.assertGreater(session.stats['spark_earned'], 0)
        self.assertGreater(session.stats['avg_quality_score'], 0.0)

    def test_report_result_failed_increments_tasks_failed(
            self, mock_emit, mock_peer):
        session = self._connected_session()
        result = {
            'status': 'error',
            'changes': [],
            'test_results': None,
            'error': 'Boom',
            'duration_s': 1.0,
            'complexity_score': 0,
        }
        session.report_result('t1', result)

        self.assertEqual(session.stats['tasks_failed'], 1)
        self.assertEqual(session.stats['tasks_completed'], 0)

    def test_report_result_spark_for_completed_task(
            self, mock_emit, mock_peer):
        session = self._connected_session()
        result = {
            'status': 'completed',
            'changes': [{'file': 'a.py', 'diff': '+x'}],
            'test_results': '5 passed',
            'duration_s': 3.0,
            'complexity_score': 2,
        }
        session.report_result('t1', result)

        # base=10 + complexity(2)*5 = 20; quality=0.5+0.2+0.3=1.0 >= 0.8 => *1.5 = 30
        self.assertGreaterEqual(session.stats['spark_earned'], SPARK_BASE_REWARD)

    def test_report_result_no_spark_for_error(
            self, mock_emit, mock_peer):
        session = self._connected_session()
        result = {
            'status': 'error',
            'changes': [],
            'test_results': None,
            'error': 'oops',
        }
        session.report_result('t1', result)

        self.assertEqual(session.stats['spark_earned'], 0)

    def test_report_result_returns_false_when_disconnected(
            self, mock_emit, mock_peer):
        session = ClaudeHiveSession()
        ok = session.report_result('t1', {'status': 'completed'})
        self.assertFalse(ok)

    def test_report_result_emits_event(self, mock_emit, mock_peer):
        session = self._connected_session()
        # Patch _emit_event on the instance so we can capture the topic
        emitted_topics = []
        original_emit = session._emit_event

        def tracking_emit(topic, data):
            emitted_topics.append(topic)
            original_emit(topic, data)

        session._emit_event = tracking_emit

        result = {
            'status': 'completed',
            'changes': [],
            'test_results': None,
        }
        session.report_result('t1', result)

        # report_result() calls _emit_event with EVENT_TASK_COMPLETED
        self.assertIn(EVENT_TASK_COMPLETED, emitted_topics)

    def test_report_result_quality_score_averages(
            self, mock_emit, mock_peer):
        session = self._connected_session()

        for i in range(3):
            result = {
                'status': 'completed',
                'changes': [{'file': 'a.py', 'diff': '+x'}],
                'test_results': None,
                'complexity_score': 1,
            }
            session.report_result(f't{i}', result)

        self.assertEqual(session.stats['tasks_completed'], 3)
        self.assertGreater(session.stats['avg_quality_score'], 0.0)
        self.assertLessEqual(session.stats['avg_quality_score'], 1.0)


@patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
@patch(f'{_PATCH_PREFIX}.emit_event', create=True)
class TestClaudeHiveSessionStatus(unittest.TestCase):
    """Tests for get_status(), pause(), resume(), set_task_scope(), get_tasks()."""

    def _connected_session(self):
        s = ClaudeHiveSession()
        s.connect(user_id='u1')
        return s

    def test_get_status_returns_correct_fields(self, mock_emit, mock_peer):
        session = self._connected_session()
        status = session.get_status()

        expected_keys = {
            'session_id', 'user_id', 'status', 'task_scope',
            'capabilities', 'current_task', 'pending_tasks',
            'completed_tasks', 'stats', 'peer_link_id',
        }
        self.assertEqual(set(status.keys()), expected_keys)
        self.assertEqual(status['status'], STATUS_IDLE)
        self.assertEqual(status['user_id'], 'u1')

    def test_pause_from_idle(self, mock_emit, mock_peer):
        session = self._connected_session()
        result = session.pause()

        self.assertTrue(result['success'])
        self.assertEqual(result['status'], STATUS_PAUSED)
        self.assertEqual(session.status, STATUS_PAUSED)

    def test_pause_when_already_paused(self, mock_emit, mock_peer):
        session = self._connected_session()
        session.pause()
        result = session.pause()

        self.assertTrue(result['success'])
        self.assertIn('Already paused', result.get('message', ''))

    def test_pause_when_disconnected(self, mock_emit, mock_peer):
        session = ClaudeHiveSession()
        result = session.pause()

        self.assertFalse(result['success'])
        self.assertIn('Not connected', result['error'])

    def test_resume_from_paused(self, mock_emit, mock_peer):
        session = self._connected_session()
        session.pause()
        result = session.resume()

        self.assertTrue(result['success'])
        self.assertEqual(result['status'], STATUS_IDLE)
        self.assertEqual(session.status, STATUS_IDLE)

    def test_resume_when_not_paused(self, mock_emit, mock_peer):
        session = self._connected_session()
        result = session.resume()

        self.assertFalse(result['success'])
        self.assertIn('Not paused', result['error'])

    def test_set_task_scope_valid(self, mock_emit, mock_peer):
        session = self._connected_session()
        result = session.set_task_scope('public')

        self.assertTrue(result['success'])
        self.assertEqual(result['scope'], 'public')
        self.assertEqual(session.task_scope, 'public')

    def test_set_task_scope_invalid(self, mock_emit, mock_peer):
        session = self._connected_session()
        result = session.set_task_scope('universe')

        self.assertFalse(result['success'])
        self.assertIn('Invalid scope', result['error'])

    def test_get_tasks_empty(self, mock_emit, mock_peer):
        session = self._connected_session()
        tasks = session.get_tasks()

        self.assertEqual(tasks['pending'], [])
        self.assertEqual(tasks['completed'], [])


# ═══════════════════════════════════════════════════════════════════════
# SessionRegistry
# ═══════════════════════════════════════════════════════════════════════

class TestSessionRegistry(unittest.TestCase):
    """Tests for SessionRegistry."""

    def setUp(self):
        self.registry = SessionRegistry()

    def test_register_adds_session(self):
        session = ClaudeHiveSession()
        session.session_id = 'chs_abc123'
        self.registry.register(session)

        self.assertIn('chs_abc123', self.registry._sessions)

    def test_register_dict(self):
        announcement = {'session_id': 'remote_1', 'status': STATUS_IDLE}
        self.registry.register(announcement)

        self.assertIn('remote_1', self.registry._sessions)

    def test_register_ignores_empty_session_id(self):
        session = ClaudeHiveSession()
        session.session_id = ''
        self.registry.register(session)

        self.assertEqual(len(self.registry._sessions), 0)

    def test_unregister_removes(self):
        session = ClaudeHiveSession()
        session.session_id = 'chs_xyz'
        self.registry.register(session)
        self.registry.unregister('chs_xyz')

        self.assertNotIn('chs_xyz', self.registry._sessions)

    def test_unregister_nonexistent_is_noop(self):
        self.registry.unregister('does_not_exist')
        # No error raised

    @patch(f'{_PATCH_PREFIX}.get_hive_session')
    def test_get_session_returns_correct_session(self, mock_get_global):
        mock_get_global.return_value = ClaudeHiveSession()
        mock_get_global.return_value.session_id = 'global'

        session = ClaudeHiveSession()
        session.session_id = 'chs_aaa'
        session.receive_task = MagicMock()  # Make it pass hasattr check
        self.registry.register(session)

        found = self.registry.get_session('chs_aaa')
        self.assertIs(found, session)

    @patch(f'{_PATCH_PREFIX}.get_hive_session')
    def test_get_session_returns_none_for_unknown(self, mock_get_global):
        mock_get_global.return_value = ClaudeHiveSession()
        mock_get_global.return_value.session_id = 'global_not_match'

        found = self.registry.get_session('nonexistent')
        self.assertIsNone(found)

    @patch(f'{_PATCH_PREFIX}.get_hive_session')
    def test_get_available_sessions_returns_idle_only(self, mock_get_global):
        # Global singleton is disconnected
        global_s = ClaudeHiveSession()
        global_s.session_id = ''
        mock_get_global.return_value = global_s

        # Register one idle and one paused
        idle_session = ClaudeHiveSession()
        idle_session.session_id = 'idle_1'
        idle_session.status = STATUS_IDLE
        idle_session.capabilities = {'languages': ['python']}
        idle_session.stats = {'avg_quality_score': 0.8}
        idle_session.task_scope = 'any'
        idle_session._peer_link_id = None

        paused_session = ClaudeHiveSession()
        paused_session.session_id = 'paused_1'
        paused_session.status = STATUS_PAUSED

        self.registry.register(idle_session)
        self.registry.register(paused_session)

        available = self.registry.get_available_sessions()
        ids = [s.get('session_id') for s in available]

        self.assertIn('idle_1', ids)
        self.assertNotIn('paused_1', ids)

    def test_thread_safety_concurrent_register_unregister(self):
        """Concurrent register/unregister should not corrupt the registry."""
        errors = []
        barrier = threading.Barrier(10)

        def worker(i):
            try:
                barrier.wait(timeout=5)
                session = ClaudeHiveSession()
                session.session_id = f'chs_thread_{i}'
                self.registry.register(session)
                # Small delay to increase contention
                time.sleep(0.001)
                self.registry.unregister(f'chs_thread_{i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        # All sessions should have been unregistered
        self.assertEqual(len(self.registry._sessions), 0)


# ═══════════════════════════════════════════════════════════════════════
# Singletons
# ═══════════════════════════════════════════════════════════════════════

class TestSingletons(unittest.TestCase):
    """Tests for module-level singleton getters."""

    def tearDown(self):
        # Reset singletons so tests are isolated
        mod._session = None
        mod._registry = None

    def test_get_hive_session_returns_same_instance(self):
        from integrations.coding_agent.claude_hive_session import get_hive_session
        a = get_hive_session()
        b = get_hive_session()
        self.assertIs(a, b)
        self.assertIsInstance(a, ClaudeHiveSession)

    def test_get_session_registry_returns_same_instance(self):
        from integrations.coding_agent.claude_hive_session import get_session_registry
        a = get_session_registry()
        b = get_session_registry()
        self.assertIs(a, b)
        self.assertIsInstance(a, SessionRegistry)


# ═══════════════════════════════════════════════════════════════════════
# Flask Blueprint
# ═══════════════════════════════════════════════════════════════════════

class TestFlaskBlueprint(unittest.TestCase):
    """Tests for the hive session Flask Blueprint and its HTTP endpoints."""

    @classmethod
    def setUpClass(cls):
        try:
            from flask import Flask
            cls.flask_available = True
        except ImportError:
            cls.flask_available = False

    def setUp(self):
        if not self.flask_available:
            self.skipTest('Flask not installed')

        from flask import Flask

        # Reset the module-level session singleton so each test is fresh
        mod._session = None
        mod._registry = None

        self.app = Flask(__name__)
        bp = create_hive_session_blueprint()
        self.assertIsNotNone(bp, "create_hive_session_blueprint() returned None")
        self.app.register_blueprint(bp)
        self.client = self.app.test_client()

    def tearDown(self):
        mod._session = None
        mod._registry = None

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_connect_endpoint_returns_200(self, mock_emit, mock_peer):
        resp = self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester', 'task_scope': 'own_repos'},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertIn('session_id', data)

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_connect_endpoint_missing_user_id_returns_400(
            self, mock_emit, mock_peer):
        resp = self.client.post(
            '/api/hive/session/connect',
            json={},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['success'])

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_disconnect_endpoint_returns_200(self, mock_emit, mock_peer):
        # Connect first
        self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester'},
        )
        resp = self.client.post('/api/hive/session/disconnect')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_status_endpoint_returns_session_info(self, mock_emit, mock_peer):
        # Connect first
        self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester'},
        )
        resp = self.client.get('/api/hive/session/status')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], STATUS_IDLE)
        self.assertEqual(data['user_id'], 'tester')

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_pause_resume_endpoints(self, mock_emit, mock_peer):
        self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester'},
        )

        resp = self.client.post('/api/hive/session/pause')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

        resp = self.client.post('/api/hive/session/resume')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_scope_endpoint(self, mock_emit, mock_peer):
        self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester'},
        )

        resp = self.client.post(
            '/api/hive/session/scope',
            json={'scope': 'public'},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['scope'], 'public')

    @patch(f'{_PATCH_PREFIX}.get_link_manager', create=True, side_effect=ImportError)
    @patch(f'{_PATCH_PREFIX}.emit_event', create=True)
    def test_tasks_endpoint(self, mock_emit, mock_peer):
        self.client.post(
            '/api/hive/session/connect',
            json={'user_id': 'tester'},
        )

        resp = self.client.get('/api/hive/session/tasks')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('pending', data)
        self.assertIn('completed', data)


# ═══════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):
    """Verify STATUS_*, EVENT_*, SPARK_* constants exist and have expected values."""

    def test_status_constants(self):
        self.assertEqual(STATUS_DISCONNECTED, 'disconnected')
        self.assertEqual(STATUS_CONNECTING, 'connecting')
        self.assertEqual(STATUS_IDLE, 'idle')
        self.assertEqual(STATUS_WORKING, 'working')
        self.assertEqual(STATUS_PAUSED, 'paused')

    def test_event_constants(self):
        self.assertTrue(EVENT_TASK_DISPATCHED.startswith('hive.'))
        self.assertTrue(EVENT_TASK_COMPLETED.startswith('hive.'))
        self.assertTrue(EVENT_SESSION_CONNECTED.startswith('hive.'))
        self.assertTrue(EVENT_SESSION_DISCONNECTED.startswith('hive.'))

    def test_spark_constants(self):
        self.assertIsInstance(SPARK_BASE_REWARD, int)
        self.assertGreater(SPARK_BASE_REWARD, 0)
        self.assertIsInstance(SPARK_COMPLEXITY_MULTIPLIER, int)
        self.assertGreater(SPARK_COMPLEXITY_MULTIPLIER, 0)
        self.assertIsInstance(SPARK_QUALITY_BONUS_THRESHOLD, float)
        self.assertGreater(SPARK_QUALITY_BONUS_THRESHOLD, 0.0)
        self.assertLessEqual(SPARK_QUALITY_BONUS_THRESHOLD, 1.0)

    def test_max_pending_tasks_positive(self):
        self.assertIsInstance(MAX_PENDING_TASKS, int)
        self.assertGreater(MAX_PENDING_TASKS, 0)

    def test_task_scopes_set(self):
        self.assertIn('own_repos', TASK_SCOPES)
        self.assertIn('public', TASK_SCOPES)
        self.assertIn('any', TASK_SCOPES)

    def test_peer_type(self):
        self.assertEqual(PEER_TYPE, 'CODING_AGENT')


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers (quality, spark, complexity)
# ═══════════════════════════════════════════════════════════════════════

class TestInternalScoring(unittest.TestCase):
    """Tests for _compute_quality_score, _calculate_spark_reward, _score_complexity."""

    def setUp(self):
        self.session = ClaudeHiveSession()

    def test_quality_score_zero_for_error(self):
        result = {'status': 'error', 'changes': [], 'test_results': None}
        score = self.session._compute_quality_score(result)
        self.assertEqual(score, 0.0)

    def test_quality_score_base_for_completed_no_changes(self):
        result = {'status': 'completed', 'changes': [], 'test_results': None}
        score = self.session._compute_quality_score(result)
        self.assertEqual(score, 0.5)

    def test_quality_score_with_changes_and_passing_tests(self):
        result = {
            'status': 'completed',
            'changes': [{'file': 'a.py'}],
            'test_results': '10 passed, 0 failed',
        }
        score = self.session._compute_quality_score(result)
        # 0.5 + 0.2 (changes) + 0.1 (passed but also has "failed" word) = 0.8
        self.assertGreaterEqual(score, 0.7)

    def test_quality_score_capped_at_1(self):
        result = {
            'status': 'completed',
            'changes': [{'file': 'a.py'}],
            'test_results': '5 passed',
        }
        score = self.session._compute_quality_score(result)
        self.assertLessEqual(score, 1.0)

    def test_spark_reward_zero_for_error(self):
        result = {'status': 'error', 'complexity_score': 5}
        reward = self.session._calculate_spark_reward(result, 0.0)
        self.assertEqual(reward, 0)

    def test_spark_reward_base_plus_complexity(self):
        result = {'status': 'completed', 'complexity_score': 2}
        reward = self.session._calculate_spark_reward(result, 0.5)
        # base=10 + 2*5 = 20, quality 0.5 < 0.8 threshold so no bonus
        self.assertEqual(reward, 20)

    def test_spark_reward_quality_bonus(self):
        result = {'status': 'completed', 'complexity_score': 2}
        reward = self.session._calculate_spark_reward(result, 0.9)
        # base=10 + 2*5 = 20, quality >= 0.8 => *1.5 = 30
        self.assertEqual(reward, 30)

    def test_complexity_score_range(self):
        # Empty
        score = self.session._score_complexity([], [])
        self.assertGreaterEqual(score, 1)
        self.assertLessEqual(score, 10)

        # Lots of files and changes
        files = ['a.py', 'b.py', 'c.py', 'd.py']
        changes = [{'diff': '\n'.join(['+line'] * 50)} for _ in range(5)]
        score = self.session._score_complexity(files, changes)
        self.assertGreaterEqual(score, 1)
        self.assertLessEqual(score, 10)


if __name__ == '__main__':
    unittest.main()
