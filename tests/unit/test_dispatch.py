"""Comprehensive tests for integrations.agent_engine.dispatch module.

Tests cover:
- mark_user_chat_activity / is_user_recently_active: timestamp tracking
- dispatch_goal: budget gate, guardrail gate, audit log, prompt_id, body, create_agent, autonomous
- dispatch_goal_distributed: coordinator check, peer check, task decomposition
- _decompose_goal: subtask extraction, single-task fallback
- _get_distributed_coordinator: Redis availability check
- _has_hive_peers: peer count check
- _notify_watchdog_llm_start/end: thread name matching
- drain_instruction_queue: wave execution, dependency ordering
- User priority gate: daemon yields when user is active
- Semaphore behavior: timeout, release
"""

import hashlib
import threading
import time
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# We need to mock heavy imports before importing the module
# Patch the modules that dispatch.py imports at top level

_pooled_post_mock = MagicMock()
_get_port_mock = MagicMock(return_value=5000)

with mock.patch.dict('sys.modules', {
    'core.http_pool': MagicMock(pooled_post=_pooled_post_mock),
    'core.port_registry': MagicMock(get_port=_get_port_mock),
}):
    import importlib
    # Force fresh import
    import integrations.agent_engine.dispatch as dispatch_mod
    # Re-bind the module-level references after patching
    dispatch_mod.pooled_post = _pooled_post_mock
    dispatch_mod.get_port = _get_port_mock


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_user_activity():
    """Reset the global user activity timestamp before each test."""
    dispatch_mod._last_user_chat_at = 0.0
    yield
    dispatch_mod._last_user_chat_at = 0.0


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Ensure the semaphore is fully released before each test."""
    # Replace with a fresh semaphore to avoid cross-test leakage
    dispatch_mod._local_llm_semaphore = threading.Semaphore(
        dispatch_mod._LOCAL_LLM_MAX_CONCURRENT
    )
    yield


@pytest.fixture(autouse=True)
def _reset_pooled_post():
    """Reset the pooled_post mock before each test."""
    _pooled_post_mock.reset_mock()
    yield


# ── mark_user_chat_activity / is_user_recently_active ─────────────────────

class TestUserActivityTracking:
    def test_initially_not_active(self):
        """No activity recorded yet -> not recently active."""
        assert dispatch_mod.is_user_recently_active() is False

    def test_mark_activity_makes_active(self):
        """After marking activity, user should be recently active."""
        dispatch_mod.mark_user_chat_activity()
        assert dispatch_mod.is_user_recently_active() is True

    def test_activity_expires_after_cooldown(self):
        """Activity should expire after _USER_CHAT_COOLDOWN seconds."""
        dispatch_mod._last_user_chat_at = time.time() - dispatch_mod._USER_CHAT_COOLDOWN - 1
        assert dispatch_mod.is_user_recently_active() is False

    def test_activity_within_cooldown(self):
        """Activity within cooldown window is still active."""
        dispatch_mod._last_user_chat_at = time.time() - (dispatch_mod._USER_CHAT_COOLDOWN / 2)
        assert dispatch_mod.is_user_recently_active() is True

    def test_mark_updates_timestamp(self):
        """mark_user_chat_activity updates the global timestamp."""
        before = time.time()
        dispatch_mod.mark_user_chat_activity()
        after = time.time()
        assert before <= dispatch_mod._last_user_chat_at <= after


# ── Deterministic prompt_id ───────────────────────────────────────────────

class TestPromptIdGeneration:
    def _compute_prompt_id(self, goal_id: str) -> str:
        """Replicate the prompt_id generation logic from dispatch_goal."""
        goal_hash = int(hashlib.md5(goal_id.encode()).hexdigest()[:10], 16) % 100_000_000_000
        return str(max(1, goal_hash))

    def test_deterministic_same_goal(self):
        """Same goal_id always produces the same prompt_id."""
        pid1 = self._compute_prompt_id('goal_abc_123')
        pid2 = self._compute_prompt_id('goal_abc_123')
        assert pid1 == pid2

    def test_different_goals_different_ids(self):
        """Different goal_ids produce different prompt_ids."""
        pid1 = self._compute_prompt_id('goal_abc_123')
        pid2 = self._compute_prompt_id('goal_xyz_456')
        assert pid1 != pid2

    def test_prompt_id_is_numeric_string(self):
        """prompt_id must be a numeric string (passes isdigit())."""
        pid = self._compute_prompt_id('test_goal')
        assert pid.isdigit()

    def test_prompt_id_minimum_is_1(self):
        """prompt_id is at least 1 (max(1, hash))."""
        pid = self._compute_prompt_id('anything')
        assert int(pid) >= 1


# ── dispatch_goal ─────────────────────────────────────────────────────────

def _make_guardrail_patches(allowed=True, reason='ok'):
    """Return a dict of patches for guardrails that allow/block dispatch."""
    enforcer = MagicMock()
    enforcer.before_dispatch.return_value = (allowed, reason, 'prompt_text')
    enforcer.after_response.return_value = (True, 'ok')
    return {
        'integrations.agent_engine.dispatch.GuardrailEnforcer': enforcer,
    }


class TestDispatchGoalBudgetGate:
    @patch('integrations.agent_engine.dispatch.is_user_recently_active', return_value=False)
    def test_budget_gate_blocks(self, _mock_active):
        """dispatch_goal returns None when budget gate blocks."""
        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(False, 'over budget'))
            ),
        }):
            # Re-import to pick up mocked module
            result = dispatch_mod.dispatch_goal('do stuff', 'u1', 'g1', 'marketing')
        assert result is None

    def test_budget_gate_import_error_passes(self):
        """When budget_gate module is missing, dispatch proceeds past budget gate.

        We verify this by letting budget gate raise ImportError (caught),
        then having guardrails block -- proving budget gate did NOT block.
        """
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (False, 'guardrail blocked', 'prompt')

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': None,  # ImportError
            'security.hive_guardrails': mock_guardrails,
        }):
            result = dispatch_mod.dispatch_goal('do stuff', 'u1', 'g1')
        # Guardrails blocked (not budget gate) -> returns None
        assert result is None
        mock_guardrails.GuardrailEnforcer.before_dispatch.assert_called_once()


class TestDispatchGoalGuardrailGate:
    def test_guardrail_import_error_blocks(self):
        """If hive_guardrails is not importable, dispatch is blocked (fail-closed)."""
        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': None,  # simulate ImportError
        }):
            result = dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')
        assert result is None

    def test_guardrail_blocks_dispatch(self):
        """Guardrail before_dispatch returning False blocks dispatch."""
        mock_budget = MagicMock(
            pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
        )
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (False, 'unsafe', 'prompt')

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': mock_budget,
            'security.hive_guardrails': mock_guardrails,
        }):
            result = dispatch_mod.dispatch_goal('bad prompt', 'u1', 'g1')
        assert result is None


class TestDispatchGoalAuditLog:
    @patch('integrations.agent_engine.dispatch.is_user_recently_active', return_value=False)
    def test_audit_log_called(self, _mock_active):
        """Audit log records goal dispatch event."""
        mock_audit = MagicMock()
        mock_audit_log_instance = MagicMock()
        mock_audit.get_audit_log.return_value = mock_audit_log_instance

        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')

        mock_budget = MagicMock(
            pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
        )

        # Mock hevolve_chat to return a response (Tier 1)
        mock_chat = MagicMock(return_value={'text': 'done'})

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': mock_budget,
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': mock_audit,
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            # Also mock distributed coordinator to return None (no distribution)
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                dispatch_mod.dispatch_goal('prompt', 'u1', 'g1', 'marketing')

        mock_audit_log_instance.log_event.assert_called_once_with(
            'goal_dispatched', actor_id='u1',
            action='dispatch marketing goal g1',
            target_id='g1',
        )


class TestDispatchGoalBodyConstruction:
    @patch('integrations.agent_engine.dispatch.is_user_recently_active', return_value=False)
    def test_tier1_body_has_required_fields(self, _mock_active):
        """Tier 1 dispatch passes correct kwargs to hevolve_chat."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'the prompt')

        mock_chat = MagicMock(return_value={'text': 'response'})

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                dispatch_mod.dispatch_goal('the prompt', 'u1', 'g1', 'coding')

        mock_chat.assert_called_once()
        call_kwargs = mock_chat.call_args
        # Check keyword args
        kw = call_kwargs.kwargs if call_kwargs.kwargs else {}
        if not kw:
            # Might be positional via keyword
            kw = call_kwargs[1] if len(call_kwargs) > 1 else {}

        assert kw.get('create_agent') is True
        assert kw.get('autonomous') is True
        assert kw.get('casual_conv') is False
        assert kw.get('user_id') == 'u1'
        assert kw.get('text') == 'the prompt'


class TestDispatchGoalUserPriority:
    def test_user_active_defers_dispatch(self):
        """When user recently chatted, dispatch defers and returns None."""
        dispatch_mod.mark_user_chat_activity()

        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')

        mock_chat = MagicMock(return_value={'text': 'done'})

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                result = dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        assert result is None
        mock_chat.assert_not_called()


class TestDispatchGoalSemaphore:
    def test_semaphore_timeout_returns_none(self):
        """When semaphore cannot be acquired (LLM busy), returns None."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')

        mock_chat = MagicMock(return_value={'text': 'done'})

        # Exhaust the semaphore before the call
        dispatch_mod._local_llm_semaphore = threading.Semaphore(0)

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                with patch.object(dispatch_mod, 'is_user_recently_active', return_value=False):
                    # Patch semaphore timeout to be very short
                    original_sem = dispatch_mod._local_llm_semaphore
                    result = dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        assert result is None
        mock_chat.assert_not_called()

    @patch('integrations.agent_engine.dispatch.is_user_recently_active', return_value=False)
    def test_semaphore_released_after_success(self, _mock_active):
        """Semaphore is released after successful dispatch."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')
        mock_chat = MagicMock(return_value={'text': 'done'})

        dispatch_mod._local_llm_semaphore = threading.Semaphore(1)

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        # Semaphore should be acquirable again (was released)
        assert dispatch_mod._local_llm_semaphore.acquire(timeout=0.1) is True

    @patch('integrations.agent_engine.dispatch.is_user_recently_active', return_value=False)
    def test_semaphore_released_after_exception(self, _mock_active):
        """Semaphore is released even if hevolve_chat raises an exception."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')
        mock_chat = MagicMock(side_effect=RuntimeError('LLM exploded'))

        dispatch_mod._local_llm_semaphore = threading.Semaphore(1)

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
                dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        # Semaphore should still be acquirable (released in finally block)
        assert dispatch_mod._local_llm_semaphore.acquire(timeout=0.1) is True


# ── dispatch_goal_distributed ─────────────────────────────────────────────

class TestDispatchGoalDistributed:
    def test_no_coordinator_returns_none(self):
        """Returns None when coordinator is unavailable."""
        with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None):
            result = dispatch_mod.dispatch_goal_distributed('prompt', 'u1', 'g1', 'marketing')
        assert result is None

    def test_coordinator_submit_succeeds(self):
        """Returns distributed goal_id on successful submission."""
        mock_coord = MagicMock()
        mock_coord.submit_goal.return_value = 'dist_g1_abc'

        with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=mock_coord):
            with patch.object(dispatch_mod, '_decompose_goal', return_value=[
                {'task_id': 'g1_task_0', 'description': 'do stuff', 'capabilities': ['marketing']}
            ]):
                result = dispatch_mod.dispatch_goal_distributed('prompt', 'u1', 'g1', 'marketing')

        assert result == 'dist_g1_abc'
        mock_coord.submit_goal.assert_called_once()
        call_kw = mock_coord.submit_goal.call_args.kwargs
        assert call_kw['objective'] == 'prompt'[:200]
        assert len(call_kw['decomposed_tasks']) == 1
        assert call_kw['context']['goal_type'] == 'marketing'
        assert call_kw['context']['user_id'] == 'u1'
        assert call_kw['context']['task_source'] == 'hive'

    def test_coordinator_submit_exception_returns_none(self):
        """Returns None when coordinator.submit_goal raises."""
        mock_coord = MagicMock()
        mock_coord.submit_goal.side_effect = RuntimeError('redis down')

        with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=mock_coord):
            with patch.object(dispatch_mod, '_decompose_goal', return_value=[
                {'task_id': 'g1_task_0', 'description': 'x', 'capabilities': ['marketing']}
            ]):
                result = dispatch_mod.dispatch_goal_distributed('prompt', 'u1', 'g1')
        assert result is None


# ── _decompose_goal ───────────────────────────────────────────────────────

class TestDecomposeGoal:
    def test_fallback_single_task(self):
        """When parallel_dispatch import fails, returns single task."""
        with patch.dict('sys.modules', {
            'integrations.agent_engine.parallel_dispatch': None,
        }):
            tasks = dispatch_mod._decompose_goal('do marketing', 'g1', 'marketing', 'u1')

        assert len(tasks) == 1
        assert tasks[0]['task_id'] == 'g1_task_0'
        assert tasks[0]['description'] == 'do marketing'
        assert tasks[0]['capabilities'] == ['marketing']

    def test_fallback_truncates_description(self):
        """Fallback truncates description to 500 chars."""
        long_prompt = 'x' * 1000
        with patch.dict('sys.modules', {
            'integrations.agent_engine.parallel_dispatch': None,
        }):
            tasks = dispatch_mod._decompose_goal(long_prompt, 'g1', 'marketing', 'u1')

        assert len(tasks[0]['description']) == 500

    def test_subtask_extraction_success(self):
        """When parallel_dispatch is available, uses its decomposition."""
        mock_parallel = MagicMock()
        mock_parallel.extract_subtasks_from_context.return_value = [
            {'name': 'sub1'}, {'name': 'sub2'}
        ]
        mock_parallel.decompose_goal_to_ledger.return_value = (
            [
                {'task_id': 'g1_t0', 'description': 'sub1', 'capabilities': ['marketing']},
                {'task_id': 'g1_t1', 'description': 'sub2', 'capabilities': ['marketing']},
            ],
            MagicMock(),  # ledger
        )

        with patch.dict('sys.modules', {
            'integrations.agent_engine.parallel_dispatch': mock_parallel,
        }):
            tasks = dispatch_mod._decompose_goal('goal', 'g1', 'marketing', 'u1')

        assert len(tasks) == 2


# ── _get_distributed_coordinator ──────────────────────────────────────────

class TestGetDistributedCoordinator:
    def test_returns_coordinator_when_available(self):
        """Returns coordinator object when Redis is reachable."""
        mock_coord = MagicMock()
        mock_api = MagicMock()
        mock_api._get_coordinator.return_value = mock_coord

        with patch.dict('sys.modules', {
            'integrations.distributed_agent': MagicMock(),
            'integrations.distributed_agent.api': mock_api,
        }):
            result = dispatch_mod._get_distributed_coordinator()
        assert result == mock_coord

    def test_returns_none_on_exception(self):
        """Returns None when Redis/coordinator is unavailable."""
        mock_api = MagicMock()
        mock_api._get_coordinator.side_effect = ConnectionError('redis down')

        with patch.dict('sys.modules', {
            'integrations.distributed_agent': MagicMock(),
            'integrations.distributed_agent.api': mock_api,
        }):
            result = dispatch_mod._get_distributed_coordinator()
        assert result is None


# ── _has_hive_peers ───────────────────────────────────────────────────────

class TestHasHivePeers:
    def test_no_peers_returns_false(self):
        """Returns False when peer count <= 1 (only self)."""
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.count.return_value = 1

        mock_models = MagicMock()
        mock_models.db_session.return_value = mock_db

        with patch.dict('sys.modules', {
            'integrations.social': MagicMock(),
            'integrations.social.models': mock_models,
        }):
            result = dispatch_mod._has_hive_peers()
        assert result is False

    def test_has_peers_returns_true(self):
        """Returns True when peer count > 1."""
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        mock_models = MagicMock()
        mock_models.db_session.return_value = mock_db

        with patch.dict('sys.modules', {
            'integrations.social': MagicMock(),
            'integrations.social.models': mock_models,
        }):
            result = dispatch_mod._has_hive_peers()
        assert result is True

    def test_exception_returns_false(self):
        """Returns False on any exception (e.g., no DB)."""
        with patch.dict('sys.modules', {
            'integrations.social': None,
            'integrations.social.models': None,
        }):
            result = dispatch_mod._has_hive_peers()
        assert result is False


# ── _notify_watchdog_llm_start / _notify_watchdog_llm_end ─────────────────

class TestNotifyWatchdog:
    def test_llm_start_marks_daemon(self):
        """_notify_watchdog_llm_start marks the daemon in watchdog."""
        mock_wd = MagicMock()
        mock_wd.is_registered.return_value = True
        mock_watchdog_mod = MagicMock()
        mock_watchdog_mod.get_watchdog.return_value = mock_wd

        # Set thread name to include daemon name
        original_name = threading.current_thread().name
        threading.current_thread().name = 'coding_daemon_worker'
        try:
            with patch.dict('sys.modules', {
                'security': MagicMock(),
                'security.node_watchdog': mock_watchdog_mod,
            }):
                dispatch_mod._notify_watchdog_llm_start()
            mock_wd.mark_in_llm_call.assert_called_with('coding_daemon')
        finally:
            threading.current_thread().name = original_name

    def test_llm_end_clears_and_heartbeats(self):
        """_notify_watchdog_llm_end clears markers and sends heartbeats."""
        mock_wd = MagicMock()
        mock_watchdog_mod = MagicMock()
        mock_watchdog_mod.get_watchdog.return_value = mock_wd

        with patch.dict('sys.modules', {
            'security': MagicMock(),
            'security.node_watchdog': mock_watchdog_mod,
        }):
            dispatch_mod._notify_watchdog_llm_end()

        # Called for both coding_daemon and agent_daemon
        assert mock_wd.clear_llm_call.call_count == 2
        assert mock_wd.heartbeat.call_count == 2

    def test_llm_start_no_watchdog(self):
        """_notify_watchdog_llm_start handles None watchdog gracefully."""
        mock_watchdog_mod = MagicMock()
        mock_watchdog_mod.get_watchdog.return_value = None

        with patch.dict('sys.modules', {
            'security': MagicMock(),
            'security.node_watchdog': mock_watchdog_mod,
        }):
            # Should not raise
            dispatch_mod._notify_watchdog_llm_start()

    def test_llm_start_import_error(self):
        """_notify_watchdog_llm_start handles ImportError gracefully."""
        with patch.dict('sys.modules', {
            'security.node_watchdog': None,
        }):
            # Should not raise
            dispatch_mod._notify_watchdog_llm_start()

    def test_llm_end_import_error(self):
        """_notify_watchdog_llm_end handles ImportError gracefully."""
        with patch.dict('sys.modules', {
            'security.node_watchdog': None,
        }):
            # Should not raise
            dispatch_mod._notify_watchdog_llm_end()

    def test_llm_start_agent_daemon_thread(self):
        """Thread named agent_daemon is correctly identified."""
        mock_wd = MagicMock()
        mock_wd.is_registered.return_value = False
        mock_watchdog_mod = MagicMock()
        mock_watchdog_mod.get_watchdog.return_value = mock_wd

        original_name = threading.current_thread().name
        threading.current_thread().name = 'agent_daemon_tick'
        try:
            with patch.dict('sys.modules', {
                'security': MagicMock(),
                'security.node_watchdog': mock_watchdog_mod,
            }):
                dispatch_mod._notify_watchdog_llm_start()
            mock_wd.mark_in_llm_call.assert_called_with('agent_daemon')
        finally:
            threading.current_thread().name = original_name


# ── drain_instruction_queue ───────────────────────────────────────────────

class TestDrainInstructionQueue:
    def _make_instruction(self, iid, text='do stuff'):
        inst = SimpleNamespace(id=iid, text=text)
        return inst

    def test_empty_queue_returns_none(self):
        """Returns None when queue has no instructions."""
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.return_value = None
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')
        assert result is None

    def test_drain_lock_contention(self):
        """Returns None when drain lock is held by another caller."""
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = False
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')
        assert result is None
        # Drain lock should NOT be released since we never acquired it
        mock_q.release_drain_lock.assert_not_called()

    def test_single_wave_single_instruction(self):
        """Dispatches a single instruction in a single wave."""
        inst = self._make_instruction('i1', 'hello')
        plan = SimpleNamespace(
            waves=[[inst]],
            total_instructions=1,
            batch_id='batch_001',
        )
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.return_value = plan
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        # Mock the HTTP call
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'result_text'}
        _pooled_post_mock.return_value = mock_resp

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')

        assert result == 'result_text'
        mock_q.complete_instruction.assert_called_once()

    def test_multi_wave_dependency_ordering(self):
        """Waves execute sequentially, instructions within a wave are parallel."""
        inst_a = self._make_instruction('a', 'task a')
        inst_b = self._make_instruction('b', 'task b')
        inst_c = self._make_instruction('c', 'task c (depends on a,b)')

        plan = SimpleNamespace(
            waves=[[inst_a, inst_b], [inst_c]],
            total_instructions=3,
            batch_id='batch_002',
        )
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.return_value = plan
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'ok'}
        _pooled_post_mock.return_value = mock_resp

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')

        assert result is not None
        assert mock_q.complete_instruction.call_count == 3

    def test_failed_instruction_recorded(self):
        """Failed HTTP calls are recorded via fail_instruction."""
        inst = self._make_instruction('i1', 'hello')
        plan = SimpleNamespace(
            waves=[[inst]],
            total_instructions=1,
            batch_id='batch_003',
        )
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.return_value = plan
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        _pooled_post_mock.return_value = mock_resp

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')

        assert result is None
        mock_q.fail_instruction.assert_called_once_with('i1', 'HTTP 500')

    def test_all_fail_returns_none(self):
        """Returns None when all instructions fail."""
        inst = self._make_instruction('i1')
        plan = SimpleNamespace(
            waves=[[inst]],
            total_instructions=1,
            batch_id='batch_004',
        )
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.return_value = plan
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        import requests as req_mod
        _pooled_post_mock.side_effect = req_mod.RequestException('connection refused')

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')

        assert result is None

        # Reset side_effect
        _pooled_post_mock.side_effect = None

    def test_drain_lock_always_released(self):
        """Drain lock is released even if execution raises."""
        mock_q = MagicMock()
        mock_q.acquire_drain_lock.return_value = True
        mock_q.pull_execution_plan.side_effect = RuntimeError('boom')
        mock_queue_mod = MagicMock()
        mock_queue_mod.get_queue.return_value = mock_q

        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': mock_queue_mod,
        }):
            # The outer try/except in drain_instruction_queue catches this
            result = dispatch_mod.drain_instruction_queue('u1')

        # Lock released in finally block
        mock_q.release_drain_lock.assert_called_once()

    def test_module_import_error_returns_none(self):
        """Returns None gracefully when instruction_queue module is missing."""
        with patch.dict('sys.modules', {
            'integrations.agent_engine.instruction_queue': None,
        }):
            result = dispatch_mod.drain_instruction_queue('u1')
        assert result is None


# ── _check_robot_capability_match ─────────────────────────────────────────

class TestRobotCapabilityMatch:
    def test_non_robot_always_passes(self):
        """Non-robot goal types always return True."""
        assert dispatch_mod._check_robot_capability_match('marketing', 'g1') is True
        assert dispatch_mod._check_robot_capability_match('coding', 'g2') is True

    def test_robot_exception_returns_true(self):
        """Robot goals return True when DB/capability check fails."""
        with patch.dict('sys.modules', {
            'integrations.social': None,
        }):
            assert dispatch_mod._check_robot_capability_match('robot', 'g1') is True


# ── Integration: dispatch_goal distributed path ────────────────────────────

class TestDispatchGoalDistributedPath:
    def test_distributed_dispatch_when_peers_exist(self):
        """dispatch_goal uses distributed path when coordinator + peers available."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
        }):
            mock_coord = MagicMock()
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=mock_coord):
                with patch.object(dispatch_mod, '_has_hive_peers', return_value=True):
                    with patch.object(dispatch_mod, 'dispatch_goal_distributed', return_value='dist_id') as mock_dist:
                        with patch.object(dispatch_mod, '_check_robot_capability_match', return_value=True):
                            result = dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        assert result == 'dist_id'
        mock_dist.assert_called_once()

    def test_distributed_fallback_to_local(self):
        """Falls back to local dispatch when distributed returns None."""
        mock_guardrails = MagicMock()
        mock_guardrails.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')
        mock_chat = MagicMock(return_value={'text': 'local_result'})

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))
            ),
            'security.hive_guardrails': mock_guardrails,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock())
            ),
            'routes.hartos_backend_adapter': MagicMock(chat=mock_chat),
        }):
            mock_coord = MagicMock()
            with patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=mock_coord):
                with patch.object(dispatch_mod, '_has_hive_peers', return_value=True):
                    with patch.object(dispatch_mod, 'dispatch_goal_distributed', return_value=None):
                        with patch.object(dispatch_mod, '_check_robot_capability_match', return_value=True):
                            with patch.object(dispatch_mod, 'is_user_recently_active', return_value=False):
                                result = dispatch_mod.dispatch_goal('prompt', 'u1', 'g1')

        assert result == 'local_result'
