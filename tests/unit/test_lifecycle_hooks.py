"""
Comprehensive tests for lifecycle_hooks.py — ActionState state machine,
lifecycle hook functions, ledger sync, retry tracking, and enforcement.
"""

import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Stub heavy transitive imports BEFORE importing lifecycle_hooks
# ---------------------------------------------------------------------------

# Stub core.session_cache.TTLCache so the module-level instantiation works
# without the real implementation (which may pull in Redis, etc.)
_real_ttl_cache_store = {}

# ---------------------------------------------------------------------------
# Save original sys.modules entries BEFORE any stubbing.
# After importing lifecycle_hooks, we IMMEDIATELY restore originals so that
# other test files collected later get the real modules (not our stubs).
# ---------------------------------------------------------------------------
_ALL_STUB_NAMES = [
    "core", "core.session_cache",
    "helper",
    "agent_ledger",
    "agent_ledger.factory",
    "core.platform",
    "core.platform.events",
    "security",
    "security.immutable_audit_log",
    "recipe_experience",
    "integrations",
    "integrations.social",
    "integrations.social.consent_service",
    "integrations.social.models",
]
_original_modules = {name: sys.modules.get(name) for name in _ALL_STUB_NAMES}


class _StubTTLCache(dict):
    """Minimal TTLCache stand-in that behaves like a dict with .get()."""

    def __init__(self, *a, **kw):
        super().__init__()
        self._loader = kw.get("loader")

    def get(self, key, default=None):
        val = super().get(key, None)
        if val is None and self._loader:
            val = self._loader(key)
            if val is not None:
                self[key] = val
        return val if val is not None else default


_session_cache_mod = types.ModuleType("core.session_cache")
_session_cache_mod.TTLCache = _StubTTLCache
sys.modules.setdefault("core", types.ModuleType("core"))
sys.modules["core.session_cache"] = _session_cache_mod

# Stub other imports that lifecycle_hooks touches at import time or runtime
for _mod_name in [
    "helper",
    "agent_ledger",
    "agent_ledger.factory",
    "core.platform",
    "core.platform.events",
    "security",
    "security.immutable_audit_log",
    "recipe_experience",
    "integrations",
    "integrations.social",
    "integrations.social.consent_service",
    "integrations.social.models",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)

# Provide PROMPTS_DIR so the import doesn't fail
sys.modules["helper"].PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "_prompts_test")

# Provide a fake TaskStatus enum for ledger sync tests
from enum import Enum as _Enum


class _FakeLedgerTaskStatus(_Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"
    DELEGATED = "delegated"
    TERMINATED = "terminated"

    @staticmethod
    def is_terminal_state(status):
        return status in (_FakeLedgerTaskStatus.COMPLETED, _FakeLedgerTaskStatus.FAILED,
                          _FakeLedgerTaskStatus.TERMINATED)


sys.modules["agent_ledger"].TaskStatus = _FakeLedgerTaskStatus

# ---------------------------------------------------------------------------
# NOW import the module under test
# ---------------------------------------------------------------------------
from lifecycle_hooks import (
    ActionState,
    FlowState,
    FlowLifecycleState,
    ActionRetryTracker,
    StateTransitionError,
    action_states,
    get_action_state,
    set_action_state,
    safe_set_state,
    validate_state_transition,
    force_state_through_valid_path,
    enforce_action_termination,
    enforce_all_actions_terminated,
    lifecycle_hook_track_action_assignment,
    lifecycle_hook_track_status_verification_request,
    lifecycle_hook_track_fallback_request,
    lifecycle_hook_track_user_fallback,
    lifecycle_hook_track_recipe_request,
    lifecycle_hook_track_recipe_completion,
    lifecycle_hook_track_termination,
    lifecycle_hook_can_increment_action,
    lifecycle_hook_check_all_actions_terminated,
    lifecycle_hook_validate_final_agent_creation,
    _auto_sync_to_ledger,
    _extract_ownership_from_prompt,
    register_ledger_for_session,
    get_registered_ledger,
    block_for_user_input,
    resume_from_user_input,
    retry_tracker,
    flow_lifecycle,
    debug_action_flow,
    validate_flow_pattern,
    initialize_deterministic_actions,
    initialize_minimal_lifecycle_hooks,
    restore_action_states_from_ledger,
    _ledger_registry,
)


# ---------------------------------------------------------------------------
# IMMEDIATELY restore sys.modules after importing lifecycle_hooks.
# The stubs were only needed for the import above; lifecycle_hooks caches
# its own references internally, so removing the stubs won't break it.
# This prevents cross-file pollution when pytest collects other test files.
# ---------------------------------------------------------------------------

def _restore_modules():
    for name, original in _original_modules.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

_restore_modules()

# After restoring modules, try to get the REAL TaskStatus so that tests
# using _FakeLedgerTaskStatus are compatible with lifecycle_hooks'
# lazy ``from agent_ledger import TaskStatus``.
try:
    from agent_ledger import TaskStatus as _LedgerTaskStatus
except (ImportError, Exception):
    # agent_ledger not installed — keep using the fake
    _LedgerTaskStatus = _FakeLedgerTaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UP = "test_user_prompt"


def _reset():
    """Clear global action_states between tests."""
    action_states.clear()
    _ledger_registry.clear()
    retry_tracker.pending_counts.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _reset()
    yield
    _reset()


def _make_action_obj(current_action, actions=None):
    """Return a simple object with .current_action and .actions."""
    obj = types.SimpleNamespace()
    obj.current_action = current_action
    obj.actions = actions or [1]
    return obj


def _make_ttl_cache(prompt, action_obj):
    """Return a dict-like object that has .get() but NOT .current_action — mimics TTLCache."""
    cache = {prompt: action_obj}

    class _Cache:
        def get(self, key, default=None):
            return cache.get(key, default)

    return _Cache()


def _make_group_chat(messages):
    return types.SimpleNamespace(messages=messages)


def _walk_to_state(prompt, action_id, target):
    """Walk action through valid transitions from ASSIGNED to *target*."""
    # Common happy-path chain
    chain = [
        ActionState.IN_PROGRESS,
        ActionState.STATUS_VERIFICATION_REQUESTED,
        ActionState.COMPLETED,
        ActionState.FALLBACK_REQUESTED,
        ActionState.FALLBACK_RECEIVED,
        ActionState.RECIPE_REQUESTED,
        ActionState.RECIPE_RECEIVED,
        ActionState.TERMINATED,
    ]
    for s in chain:
        if get_action_state(prompt, action_id) == target:
            return
        set_action_state(prompt, action_id, s, "walk")
    # Final check
    if get_action_state(prompt, action_id) != target:
        raise RuntimeError(f"Could not walk to {target}")


# ===================================================================
# 1. ActionState enum
# ===================================================================

class TestActionStateEnum:
    def test_all_values_present(self):
        expected = {
            "assigned", "in_progress", "status_verification_requested",
            "completed", "pending", "error", "fallback_requested",
            "fallback_received", "recipe_requested", "recipe_received",
            "terminated", "executing_motion", "sensor_confirm",
            "preview_pending", "preview_approved",
        }
        actual = {s.value for s in ActionState}
        assert expected == actual

    def test_enum_count(self):
        assert len(ActionState) == 15

    def test_flow_state_enum(self):
        assert FlowState.FLOW_COMPLETED.value == "flow_completed"


# ===================================================================
# 2. get_action_state / set_action_state
# ===================================================================

class TestGetSetActionState:
    def test_default_is_assigned(self):
        assert get_action_state(UP, 1) == ActionState.ASSIGNED

    def test_set_then_get(self):
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        assert get_action_state(UP, 1) == ActionState.IN_PROGRESS

    def test_idempotent_same_state(self):
        """Setting to current state is a no-op (no error)."""
        assert get_action_state(UP, 1) == ActionState.ASSIGNED
        set_action_state(UP, 1, ActionState.ASSIGNED)  # should not raise
        assert get_action_state(UP, 1) == ActionState.ASSIGNED

    def test_invalid_transition_raises(self):
        """ASSIGNED -> COMPLETED is not valid — should raise."""
        with pytest.raises(StateTransitionError):
            set_action_state(UP, 1, ActionState.COMPLETED)

    def test_thread_safety(self):
        """Concurrent set_action_state calls should not corrupt state."""
        errors = []

        def worker(aid):
            try:
                set_action_state(f"thread_{aid}", aid, ActionState.IN_PROGRESS)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ===================================================================
# 3. validate_state_transition
# ===================================================================

class TestValidateStateTransition:
    def test_assigned_to_in_progress(self):
        assert validate_state_transition(UP, 1, ActionState.IN_PROGRESS) is True

    def test_assigned_to_completed_invalid(self):
        assert validate_state_transition(UP, 1, ActionState.COMPLETED) is False

    def test_error_to_terminated_valid(self):
        """ERROR can go directly to TERMINATED."""
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        set_action_state(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        set_action_state(UP, 1, ActionState.ERROR)
        assert validate_state_transition(UP, 1, ActionState.TERMINATED) is True

    def test_terminated_to_assigned(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        assert validate_state_transition(UP, 1, ActionState.ASSIGNED) is True

    def test_preview_pending_to_approved(self):
        set_action_state(UP, 1, ActionState.PREVIEW_PENDING)
        assert validate_state_transition(UP, 1, ActionState.PREVIEW_APPROVED) is True


# ===================================================================
# 4. force_state_through_valid_path
# ===================================================================

class TestForceStateThroughValidPath:
    def test_assigned_to_in_progress(self):
        assert force_state_through_valid_path(UP, 1, ActionState.IN_PROGRESS) is True
        assert get_action_state(UP, 1) == ActionState.IN_PROGRESS

    def test_assigned_to_completed(self):
        assert force_state_through_valid_path(UP, 1, ActionState.COMPLETED) is True
        assert get_action_state(UP, 1) == ActionState.COMPLETED

    def test_same_state_returns_true(self):
        assert force_state_through_valid_path(UP, 1, ActionState.ASSIGNED) is True

    def test_no_valid_path_returns_false(self):
        # ASSIGNED -> TERMINATED has no direct path in state_paths
        assert force_state_through_valid_path(UP, 1, ActionState.TERMINATED) is False

    def test_error_to_completed(self):
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        set_action_state(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        set_action_state(UP, 1, ActionState.ERROR)
        assert force_state_through_valid_path(UP, 1, ActionState.COMPLETED) is True
        assert get_action_state(UP, 1) == ActionState.COMPLETED


# ===================================================================
# 5. safe_set_state
# ===================================================================

class TestSafeSetState:
    def test_valid_returns_true(self):
        assert safe_set_state(UP, 1, ActionState.IN_PROGRESS) is True

    def test_invalid_returns_false(self):
        assert safe_set_state(UP, 1, ActionState.TERMINATED) is False


# ===================================================================
# 6. lifecycle_hook_track_action_assignment
# ===================================================================

class TestTrackActionAssignment:
    def test_with_int(self):
        result = lifecycle_hook_track_action_assignment(UP, 1)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.ASSIGNED

    def test_with_action_object_no_group_chat(self):
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_track_action_assignment(UP, action)
        # No group_chat, so ChatInstructor check won't fire -> False
        assert result is False

    def test_with_ttl_cache_missing_prompt(self):
        cache = _make_ttl_cache("other_prompt", _make_action_obj(1))
        result = lifecycle_hook_track_action_assignment(UP, cache)
        assert result is False

    def test_with_ttl_cache_valid(self):
        action = _make_action_obj(current_action=1)
        cache = _make_ttl_cache(UP, action)
        gc = _make_group_chat([{"name": "ChatInstructor", "content": "Action 1 do something"}])
        result = lifecycle_hook_track_action_assignment(UP, cache, gc)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.IN_PROGRESS

    def test_with_none_like_object(self):
        """Object without .get or .current_action returns False."""
        result = lifecycle_hook_track_action_assignment(UP, object())
        assert result is False

    def test_locked_state_skips(self):
        """If action already IN_PROGRESS, assignment hook returns False."""
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "ChatInstructor", "content": "Action 1 go"}])
        result = lifecycle_hook_track_action_assignment(UP, action, gc)
        assert result is False


# ===================================================================
# 7. lifecycle_hook_track_status_verification_request
# ===================================================================

class TestTrackStatusVerification:
    def test_with_int(self):
        result = lifecycle_hook_track_status_verification_request(UP, 1)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.STATUS_VERIFICATION_REQUESTED

    def test_with_action_object_and_group_chat(self):
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "Calling @StatusVerifier now"}])
        result = lifecycle_hook_track_status_verification_request(UP, action, gc)
        assert result is True

    def test_with_ttl_cache_missing(self):
        cache = _make_ttl_cache("other", _make_action_obj(1))
        result = lifecycle_hook_track_status_verification_request(UP, cache)
        assert result is False

    def test_no_status_verifier_mention(self):
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "checking status"}])
        result = lifecycle_hook_track_status_verification_request(UP, action, gc)
        assert result is False


# ===================================================================
# 8. lifecycle_hook_track_fallback_request
# ===================================================================

class TestTrackFallbackRequest:
    def test_with_action_object(self):
        _walk_to_state(UP, 1, ActionState.COMPLETED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "fallback needed, ask user what to do"}])
        result = lifecycle_hook_track_fallback_request(UP, action, gc)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.FALLBACK_REQUESTED

    def test_with_ttl_cache(self):
        _walk_to_state(UP, 1, ActionState.COMPLETED)
        action = _make_action_obj(current_action=1)
        cache = _make_ttl_cache(UP, action)
        gc = _make_group_chat([{"name": "Agent", "content": "fallback required, ask user please"}])
        result = lifecycle_hook_track_fallback_request(UP, cache, gc)
        assert result is True

    def test_no_fallback_keyword(self):
        _walk_to_state(UP, 1, ActionState.COMPLETED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "everything is fine"}])
        result = lifecycle_hook_track_fallback_request(UP, action, gc)
        assert result is False

    def test_returns_false_for_unsupported_type(self):
        gc = _make_group_chat([{"name": "Agent", "content": "fallback ask user"}])
        result = lifecycle_hook_track_fallback_request(UP, 42, gc)
        assert result is False


# ===================================================================
# 9. lifecycle_hook_track_user_fallback
# ===================================================================

class TestTrackUserFallback:
    def test_with_action_object(self):
        _walk_to_state(UP, 1, ActionState.FALLBACK_REQUESTED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "UserProxy", "content": "use backup server"}])
        result = lifecycle_hook_track_user_fallback(UP, action, gc)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.FALLBACK_RECEIVED

    def test_not_in_fallback_requested_state(self):
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "UserProxy", "content": "hi"}])
        result = lifecycle_hook_track_user_fallback(UP, action, gc)
        assert result is False

    def test_with_ttl_cache(self):
        _walk_to_state(UP, 1, ActionState.FALLBACK_REQUESTED)
        action = _make_action_obj(current_action=1)
        cache = _make_ttl_cache(UP, action)
        gc = _make_group_chat([{"name": "UserProxy", "content": "retry with alt config"}])
        result = lifecycle_hook_track_user_fallback(UP, cache, gc)
        assert result is True


# ===================================================================
# 10. lifecycle_hook_track_recipe_request
# ===================================================================

class TestTrackRecipeRequest:
    def test_with_action_object(self):
        _walk_to_state(UP, 1, ActionState.FALLBACK_RECEIVED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "Focus on the current task at hand and create a detailed recipe"}])
        result = lifecycle_hook_track_recipe_request(UP, action, gc)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.RECIPE_REQUESTED

    def test_no_recipe_keyword(self):
        _walk_to_state(UP, 1, ActionState.FALLBACK_RECEIVED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "proceeding"}])
        result = lifecycle_hook_track_recipe_request(UP, action, gc)
        assert result is False

    def test_returns_false_for_unsupported_type(self):
        gc = _make_group_chat([{"name": "Agent", "content": "Focus on the current task at hand and create a detailed recipe"}])
        result = lifecycle_hook_track_recipe_request(UP, 99, gc)
        assert result is False


# ===================================================================
# 11. lifecycle_hook_track_recipe_completion
# ===================================================================

class TestTrackRecipeCompletion:
    def test_recipe_done(self):
        _walk_to_state(UP, 1, ActionState.RECIPE_REQUESTED)
        action = _make_action_obj(current_action=1)
        json_obj = {"status": "done"}
        result = lifecycle_hook_track_recipe_completion(UP, json_obj, action)
        assert result["action"] == "save_recipe_and_terminate"

    def test_no_status_key(self):
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_track_recipe_completion(UP, {}, action)
        assert result["action"] == "allow"

    def test_none_json(self):
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_track_recipe_completion(UP, None, action)
        assert result["action"] == "allow"

    def test_status_not_done(self):
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_track_recipe_completion(UP, {"status": "pending"}, action)
        assert result["action"] == "allow"

    def test_with_ttl_cache(self):
        _walk_to_state(UP, 1, ActionState.RECIPE_REQUESTED)
        action = _make_action_obj(current_action=1)
        cache = _make_ttl_cache(UP, action)
        result = lifecycle_hook_track_recipe_completion(UP, {"status": "done"}, cache)
        assert result["action"] == "save_recipe_and_terminate"


# ===================================================================
# 12. lifecycle_hook_track_termination
# ===================================================================

class TestTrackTermination:
    def test_terminate_message(self):
        _walk_to_state(UP, 1, ActionState.RECIPE_RECEIVED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "TERMINATE"}])
        result = lifecycle_hook_track_termination(UP, action, gc)
        assert result is True
        assert get_action_state(UP, 1) == ActionState.TERMINATED

    def test_no_terminate_keyword(self):
        _walk_to_state(UP, 1, ActionState.RECIPE_RECEIVED)
        action = _make_action_obj(current_action=1)
        gc = _make_group_chat([{"name": "Agent", "content": "done"}])
        result = lifecycle_hook_track_termination(UP, action, gc)
        assert result is False

    def test_returns_false_for_unsupported_type(self):
        gc = _make_group_chat([{"name": "Agent", "content": "TERMINATE"}])
        result = lifecycle_hook_track_termination(UP, 42, gc)
        assert result is False


# ===================================================================
# 13. lifecycle_hook_check_all_actions_terminated
# ===================================================================

class TestCheckAllActionsTerminated:
    def test_all_terminated(self):
        for i in range(1, 4):
            _walk_to_state(UP, i, ActionState.TERMINATED)
        action = _make_action_obj(current_action=4, actions=[1, 2, 3])
        result = lifecycle_hook_check_all_actions_terminated(UP, action)
        assert result["action"] == "create_flow_recipe"

    def test_not_all_terminated(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        # action 2 still ASSIGNED
        action = _make_action_obj(current_action=3, actions=[1, 2])
        result = lifecycle_hook_check_all_actions_terminated(UP, action)
        assert result["action"] == "block_flow_completion"

    def test_continue_actions(self):
        action = _make_action_obj(current_action=1, actions=[1, 2, 3])
        result = lifecycle_hook_check_all_actions_terminated(UP, action)
        assert result["action"] == "continue_actions"

    def test_with_ttl_cache(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        action = _make_action_obj(current_action=2, actions=[1])
        cache = _make_ttl_cache(UP, action)
        result = lifecycle_hook_check_all_actions_terminated(UP, cache)
        assert result["action"] == "create_flow_recipe"

    def test_unsupported_type(self):
        result = lifecycle_hook_check_all_actions_terminated(UP, object())
        assert result["action"] == "allow"


# ===================================================================
# 14. lifecycle_hook_validate_final_agent_creation
# ===================================================================

class TestValidateFinalAgentCreation:
    def test_all_terminated_and_files_exist(self):
        import lifecycle_hooks as _lh_mod
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        action = _make_action_obj(current_action=1, actions=[1])
        # Use the actual PROMPTS_DIR the module resolved at import time
        prompts_dir = _lh_mod.PROMPTS_DIR
        os.makedirs(prompts_dir, exist_ok=True)
        recipe_file = os.path.join(prompts_dir, "42_0_1.json")
        flow_file = os.path.join(prompts_dir, "42_0_recipe.json")
        try:
            open(recipe_file, "w").close()
            open(flow_file, "w").close()
            result = lifecycle_hook_validate_final_agent_creation(UP, action, 42)
            assert result["action"] == "allow"
        finally:
            for f in [recipe_file, flow_file]:
                if os.path.exists(f):
                    os.remove(f)

    def test_blocks_when_not_terminated(self):
        action = _make_action_obj(current_action=1, actions=[1])
        result = lifecycle_hook_validate_final_agent_creation(UP, action, 42)
        assert result["action"] == "block"

    def test_blocks_when_no_tasks(self):
        result = lifecycle_hook_validate_final_agent_creation(UP, object(), 42)
        assert result["action"] == "block"

    def test_missing_recipe_file(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        action = _make_action_obj(current_action=1, actions=[1])
        result = lifecycle_hook_validate_final_agent_creation(UP, action, 9999)
        assert result["action"] == "block"
        assert "Missing recipe files" in result["message"]


# ===================================================================
# 15. _auto_sync_to_ledger
# ===================================================================

class TestAutoSyncToLedger:
    def _make_task(self, status, is_owned=False, started_at=None):
        task = MagicMock()
        task.status = status
        task.is_owned = is_owned
        task.started_at = started_at
        task.sla_breached = False
        task.is_sla_breached.return_value = False
        return task

    def test_no_ledger_registered_skips(self):
        # Should not raise
        _auto_sync_to_ledger(UP, 1, ActionState.IN_PROGRESS)

    def test_task_not_in_ledger_skips(self):
        ledger = MagicMock()
        ledger.tasks = {}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.IN_PROGRESS)
        ledger.update_task_status.assert_not_called()

    def test_skip_noop_transition(self):
        """IN_PROGRESS -> IN_PROGRESS (same ledger status) should be skipped."""
        task = self._make_task(_LedgerTaskStatus.IN_PROGRESS)
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        # Both map to IN_PROGRESS — no-op
        ledger.update_task_status.assert_not_called()

    def test_paused_to_in_progress_resumes(self):
        task = self._make_task(_LedgerTaskStatus.PAUSED)
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.IN_PROGRESS)
        task.resume.assert_called_once()
        assert task.blocked_reason is None

    def test_blocked_to_in_progress_resumes(self):
        task = self._make_task(_LedgerTaskStatus.BLOCKED)
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.FALLBACK_RECEIVED)
        task.resume.assert_called_once()

    def test_claims_ownership_on_in_progress(self):
        task = self._make_task(_LedgerTaskStatus.PENDING, is_owned=False)
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.IN_PROGRESS)
        task.claim.assert_called_once()

    def test_sets_blocked_reason_for_preview_pending(self):
        task = self._make_task(_LedgerTaskStatus.PENDING)
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        _auto_sync_to_ledger(UP, 1, ActionState.PREVIEW_PENDING)
        task.set_blocked_reason.assert_called_with("approval_required")


# ===================================================================
# 16. enforce functions
# ===================================================================

class TestEnforcement:
    def test_enforce_action_termination_raises(self):
        with pytest.raises(StateTransitionError):
            enforce_action_termination(UP, 1)

    def test_enforce_action_termination_ok(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        enforce_action_termination(UP, 1)  # should not raise

    def test_enforce_all_terminated_true(self):
        for i in range(1, 3):
            _walk_to_state(UP, i, ActionState.TERMINATED)
        ok, msg = enforce_all_actions_terminated(UP, 2)
        assert ok is True

    def test_enforce_all_terminated_false(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        ok, msg = enforce_all_actions_terminated(UP, 2)
        assert ok is False


# ===================================================================
# 17. ActionRetryTracker
# ===================================================================

class TestActionRetryTracker:
    def test_increment_under_threshold(self):
        assert retry_tracker.increment_pending(UP, 1) is False

    def test_increment_over_threshold(self):
        for _ in range(retry_tracker.MAX_PENDING_RETRIES):
            retry_tracker.increment_pending(UP, 1)
        assert retry_tracker.increment_pending(UP, 1) is True

    def test_reset_count(self):
        retry_tracker.increment_pending(UP, 1)
        retry_tracker.reset_count(UP, 1)
        assert (UP, 1) not in retry_tracker.pending_counts


# ===================================================================
# 18. FlowLifecycleState
# ===================================================================

class TestFlowLifecycleState:
    def test_set_and_get(self):
        flow_lifecycle.set_flow_state(UP, "flow1", FlowState.DEPENDENCY_ANALYSIS)
        assert flow_lifecycle.flows[UP]["flow1"] == FlowState.DEPENDENCY_ANALYSIS
        # cleanup
        flow_lifecycle.flows.clear()


# ===================================================================
# 19. lifecycle_hook_can_increment_action
# ===================================================================

class TestCanIncrementAction:
    def test_blocks_if_not_terminated(self):
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_can_increment_action(UP, action)
        assert result["action"] == "block"

    def test_allows_if_terminated(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_can_increment_action(UP, action)
        assert result["action"] == "allow"


# ===================================================================
# 20. Helper / utility functions
# ===================================================================

class TestUtilities:
    def test_extract_ownership_from_prompt(self):
        uid, pid = _extract_ownership_from_prompt("123_456")
        assert uid == "123"
        assert pid == "456"

    def test_extract_ownership_no_underscore(self):
        uid, pid = _extract_ownership_from_prompt("single")
        assert uid == "single"
        assert pid is None

    def test_register_and_get_ledger(self):
        mock_ledger = MagicMock()
        register_ledger_for_session(UP, mock_ledger)
        assert get_registered_ledger(UP) is mock_ledger

    def test_initialize_deterministic_actions(self):
        assert initialize_deterministic_actions() is True

    def test_initialize_minimal_lifecycle_hooks(self):
        assert initialize_minimal_lifecycle_hooks() is True

    def test_validate_flow_pattern(self):
        assert validate_flow_pattern(UP, 1) == "flow_in_progress"
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        assert validate_flow_pattern(UP, 1) == "completed_flow"

    def test_debug_action_flow_not_started(self):
        # Should not raise
        debug_action_flow(UP, 99)

    def test_debug_action_flow_terminated(self):
        _walk_to_state(UP, 1, ActionState.TERMINATED)
        debug_action_flow(UP, 1)  # should not raise


# ===================================================================
# 21. block_for_user_input / resume_from_user_input
# ===================================================================

class TestBlockResumeUserInput:
    def test_block_no_ledger(self):
        block_for_user_input(UP, 1)  # should not raise

    def test_resume_no_ledger(self):
        resume_from_user_input(UP, 1)  # should not raise

    def test_block_task_not_in_progress(self):
        task = MagicMock()
        task.status = _LedgerTaskStatus.PENDING
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        block_for_user_input(UP, 1)
        task.block.assert_not_called()

    def test_resume_task_not_blocked(self):
        task = MagicMock()
        task.status = _LedgerTaskStatus.IN_PROGRESS
        ledger = MagicMock()
        ledger.tasks = {"action_1": task}
        _ledger_registry[UP] = ledger
        resume_from_user_input(UP, 1)
        task.resume.assert_not_called()


# ===================================================================
# 22. restore_action_states_from_ledger
# ===================================================================

class TestRestoreFromLedger:
    def test_restore_actions(self):
        task1 = MagicMock()
        task1.status = _LedgerTaskStatus.COMPLETED
        task2 = MagicMock()
        task2.status = _LedgerTaskStatus.FAILED
        ledger = MagicMock()
        ledger.tasks = {"action_1": task1, "action_2": task2, "not_action": MagicMock()}
        count = restore_action_states_from_ledger(UP, ledger)
        assert count == 2
        assert get_action_state(UP, 1) == ActionState.TERMINATED
        assert get_action_state(UP, 2) == ActionState.ERROR

    def test_restore_skips_bad_task_ids(self):
        task = MagicMock()
        task.status = _LedgerTaskStatus.IN_PROGRESS
        ledger = MagicMock()
        ledger.tasks = {"action_abc": task}
        count = restore_action_states_from_ledger(UP, ledger)
        assert count == 0


# ===================================================================
# 23. lifecycle_hook_process_verifier_response (bonus coverage)
# ===================================================================

class TestProcessVerifierResponse:
    def test_completed_status(self):
        from lifecycle_hooks import lifecycle_hook_process_verifier_response
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        set_action_state(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_process_verifier_response(UP, {"status": "completed"}, action)
        assert result["action"] == "force_fallback"

    def test_pending_status(self):
        from lifecycle_hooks import lifecycle_hook_process_verifier_response
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        set_action_state(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_process_verifier_response(UP, {"status": "pending"}, action)
        assert result["action"] == "force_completion"

    def test_error_status(self):
        from lifecycle_hooks import lifecycle_hook_process_verifier_response
        set_action_state(UP, 1, ActionState.IN_PROGRESS)
        set_action_state(UP, 1, ActionState.STATUS_VERIFICATION_REQUESTED)
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_process_verifier_response(UP, {"status": "error"}, action)
        assert result["action"] == "force_fallback"

    def test_no_status_key(self):
        from lifecycle_hooks import lifecycle_hook_process_verifier_response
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_process_verifier_response(UP, {"foo": "bar"}, action)
        assert result["action"] == "allow"

    def test_none_json(self):
        from lifecycle_hooks import lifecycle_hook_process_verifier_response
        action = _make_action_obj(current_action=1)
        result = lifecycle_hook_process_verifier_response(UP, None, action)
        assert result["action"] == "allow"
