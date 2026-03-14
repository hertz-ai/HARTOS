"""
Functional tests for CREATE/REUSE pipeline lifecycle.

Tests the Action class, ActionState state machine, recipe I/O, and
safe_prompt_path — all pure logic with zero external dependencies.

Run:
    pytest tests/functional/test_pipeline_lifecycle_functional.py -v --noconftest
"""

import json
import os
import re
import sys
import threading

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 1. Action class round-trip
# ---------------------------------------------------------------------------

class TestActionClassRoundtrip:
    """Test the Action data holder from helper.py."""

    def _make_actions(self, count=3):
        return [
            {'action_id': i + 1, 'action': f'Step {i + 1}', 'tool': 'test_tool'}
            for i in range(count)
        ]

    def test_action_class_roundtrip(self):
        """Create Action with 3 actions, verify get_action(), current_action
        increment, and action count via len(actions)."""
        from helper import Action

        raw = self._make_actions(3)
        action = Action(raw)

        # Initial state
        assert action.current_action == 1
        assert action.fallback is False
        assert action.recipe is False
        assert action.ledger is None
        assert len(action.actions) == 3

        # get_action by index
        assert action.get_action(0) == raw[0]
        assert action.get_action(1) == raw[1]
        assert action.get_action(2) == raw[2]

        # Out-of-range raises IndexError
        with pytest.raises(IndexError):
            action.get_action(3)
        with pytest.raises(IndexError):
            action.get_action(-1)

        # current_action increments correctly
        action.current_action = 2
        assert action.current_action == 2
        action.current_action += 1
        assert action.current_action == 3

        # get_action_byaction_id
        assert action.get_action_byaction_id(2) == raw[1]
        assert action.get_action_byaction_id(999) is None

    def test_action_new_json_accumulation(self):
        """Verify new_json list accumulates entries."""
        from helper import Action

        action = Action(self._make_actions(2))
        assert action.new_json == []

        action.new_json.append({'step': 1, 'result': 'ok'})
        action.new_json.append({'step': 2, 'result': 'ok'})
        assert len(action.new_json) == 2


# ---------------------------------------------------------------------------
# 2. ActionState state machine — happy path
# ---------------------------------------------------------------------------

class TestLifecycleStateMachine:
    """Walk an action through the full valid state sequence."""

    def setup_method(self):
        """Clear global state before each test."""
        from lifecycle_hooks import action_states
        action_states.clear()

    def test_lifecycle_state_machine(self):
        """ASSIGNED -> IN_PROGRESS -> STATUS_VERIFICATION_REQUESTED ->
        COMPLETED -> TERMINATED.  Each transition must be accepted."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
            action_states,
        )

        prompt = 'test_user_42'
        aid = 1

        # Default state is ASSIGNED
        assert get_action_state(prompt, aid) == ActionState.ASSIGNED

        # Walk through the standard lifecycle
        transitions = [
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED,
            ActionState.TERMINATED,
        ]

        for target in transitions:
            set_action_state(prompt, aid, target, reason='functional test')
            assert get_action_state(prompt, aid) == target

    def test_full_recipe_path(self):
        """ASSIGNED -> ... -> COMPLETED -> RECIPE_REQUESTED -> RECIPE_RECEIVED
        -> TERMINATED (the CREATE-mode recipe path)."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
        )

        prompt = 'recipe_path_user'
        aid = 1

        path = [
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED,
            ActionState.RECIPE_REQUESTED,
            ActionState.RECIPE_RECEIVED,
            ActionState.TERMINATED,
        ]

        for state in path:
            set_action_state(prompt, aid, state, reason='recipe path test')
            assert get_action_state(prompt, aid) == state

    def test_error_retry_path(self):
        """ASSIGNED -> IN_PROGRESS -> SVR -> ERROR -> IN_PROGRESS (retry)."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
        )

        prompt = 'error_retry_user'
        aid = 1

        path = [
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.ERROR,
            ActionState.IN_PROGRESS,  # retry
        ]

        for state in path:
            set_action_state(prompt, aid, state, reason='error retry test')
            assert get_action_state(prompt, aid) == state


# ---------------------------------------------------------------------------
# 3. Invalid state transition
# ---------------------------------------------------------------------------

class TestInvalidStateTransition:
    """Verify the state machine rejects illegal jumps."""

    def setup_method(self):
        from lifecycle_hooks import action_states
        action_states.clear()

    def test_invalid_state_transition(self):
        """ASSIGNED -> COMPLETED directly must raise StateTransitionError."""
        from lifecycle_hooks import (
            ActionState,
            StateTransitionError,
            set_action_state,
            get_action_state,
        )

        prompt = 'invalid_test_user'
        aid = 1

        assert get_action_state(prompt, aid) == ActionState.ASSIGNED

        with pytest.raises(StateTransitionError):
            set_action_state(prompt, aid, ActionState.COMPLETED, reason='skip')

        # State must remain unchanged
        assert get_action_state(prompt, aid) == ActionState.ASSIGNED

    def test_assigned_to_terminated_rejected(self):
        """ASSIGNED -> TERMINATED must be rejected (no shortcut)."""
        from lifecycle_hooks import (
            ActionState,
            StateTransitionError,
            set_action_state,
        )

        prompt = 'no_shortcut_user'
        aid = 1

        with pytest.raises(StateTransitionError):
            set_action_state(prompt, aid, ActionState.TERMINATED, reason='skip all')

    def test_in_progress_to_completed_rejected(self):
        """IN_PROGRESS -> COMPLETED is invalid (must go through SVR)."""
        from lifecycle_hooks import (
            ActionState,
            StateTransitionError,
            set_action_state,
        )

        prompt = 'skip_svr_user'
        aid = 1

        set_action_state(prompt, aid, ActionState.IN_PROGRESS)

        with pytest.raises(StateTransitionError):
            set_action_state(prompt, aid, ActionState.COMPLETED, reason='skip SVR')


# ---------------------------------------------------------------------------
# 4. Recipe save and load
# ---------------------------------------------------------------------------

class TestRecipeSaveAndLoad:
    """Write recipe JSON to a tmp directory, read back, verify structure."""

    def test_recipe_save_and_load(self, tmp_path):
        """Round-trip a recipe JSON through the filesystem."""
        recipe = {
            'prompt_id': '12345',
            'flow_id': '0',
            'actions': [
                {
                    'action_id': 1,
                    'action': 'Search for topic',
                    'tool': 'google_search',
                    'status': 'done',
                    'result': 'Found 10 results',
                },
                {
                    'action_id': 2,
                    'action': 'Summarise results',
                    'tool': 'llm',
                    'status': 'done',
                    'result': 'Summary text here',
                },
            ],
            'metadata': {
                'user_id': 'redacted',
                'execution_time': 3.14,
            },
        }

        recipe_path = tmp_path / '12345_0_recipe.json'

        # Write
        with open(recipe_path, 'w') as f:
            json.dump(recipe, f, indent=4)

        assert recipe_path.exists()

        # Read back
        with open(recipe_path, 'r') as f:
            loaded = json.load(f)

        assert loaded['prompt_id'] == '12345'
        assert loaded['flow_id'] == '0'
        assert len(loaded['actions']) == 2
        assert loaded['actions'][0]['tool'] == 'google_search'
        assert loaded['actions'][1]['status'] == 'done'
        assert loaded['metadata']['execution_time'] == pytest.approx(3.14)

    def test_recipe_overwrite(self, tmp_path):
        """Overwriting a recipe replaces the old contents completely."""
        path = tmp_path / 'overwrite_recipe.json'

        original = {'version': 1, 'actions': []}
        with open(path, 'w') as f:
            json.dump(original, f)

        updated = {'version': 2, 'actions': [{'action_id': 1}]}
        with open(path, 'w') as f:
            json.dump(updated, f)

        with open(path, 'r') as f:
            loaded = json.load(f)

        assert loaded['version'] == 2
        assert len(loaded['actions']) == 1


# ---------------------------------------------------------------------------
# 5. Prompt config round-trip
# ---------------------------------------------------------------------------

class TestPromptConfigRoundtrip:
    """Create a prompt config JSON with flows/actions, load, verify."""

    def test_prompt_config_roundtrip(self, tmp_path):
        """Full prompt config structure with nested flows and actions."""
        prompt_config = {
            'prompt_id': '99999',
            'prompt': 'Research quantum computing advances',
            'flows': [
                {
                    'flow_id': 0,
                    'persona': 'Researcher',
                    'actions': [
                        {
                            'action_id': 1,
                            'action': 'Search latest papers',
                            'tool': 'google_search',
                            'can_perform_without_user_input': 'yes',
                        },
                        {
                            'action_id': 2,
                            'action': 'Summarise findings',
                            'tool': 'llm',
                            'can_perform_without_user_input': 'yes',
                        },
                        {
                            'action_id': 3,
                            'action': 'Present to user',
                            'tool': 'send_message_to_user',
                            'can_perform_without_user_input': 'no',
                        },
                    ],
                },
                {
                    'flow_id': 1,
                    'persona': 'Fact-Checker',
                    'actions': [
                        {
                            'action_id': 1,
                            'action': 'Cross-reference claims',
                            'tool': 'google_search',
                            'can_perform_without_user_input': 'yes',
                        },
                    ],
                },
            ],
        }

        config_path = tmp_path / '99999.json'
        with open(config_path, 'w') as f:
            json.dump(prompt_config, f, indent=4)

        with open(config_path, 'r') as f:
            loaded = json.load(f)

        # Top-level
        assert loaded['prompt_id'] == '99999'
        assert 'quantum' in loaded['prompt']

        # Flow structure
        assert len(loaded['flows']) == 2
        assert loaded['flows'][0]['persona'] == 'Researcher'
        assert loaded['flows'][1]['persona'] == 'Fact-Checker'

        # Actions within flow 0
        flow0_actions = loaded['flows'][0]['actions']
        assert len(flow0_actions) == 3
        assert flow0_actions[2]['can_perform_without_user_input'] == 'no'

        # Actions within flow 1
        assert len(loaded['flows'][1]['actions']) == 1


# ---------------------------------------------------------------------------
# 6. safe_prompt_path — path traversal rejection
# ---------------------------------------------------------------------------

class TestSafePromptPathTraversal:
    """Verify path traversal in prompt_id raises ValueError."""

    def test_safe_prompt_path_traversal(self):
        """Path traversal attempts must be rejected."""
        from helper import safe_prompt_path

        traversal_payloads = [
            '../etc/passwd',
            '..\\windows\\system32',
            'foo/bar',
            'foo\\bar',
            '../../secret',
            'valid/../escape',
            'hello world',       # spaces
            'prompt;rm -rf /',   # injection
            'prompt|cat /etc',   # pipe injection
            '',                  # empty
        ]

        for payload in traversal_payloads:
            with pytest.raises(ValueError, match="Invalid path component"):
                safe_prompt_path(payload)

    def test_safe_prompt_path_traversal_in_later_parts(self):
        """Traversal in flow_id or action_id parts must also be rejected."""
        from helper import safe_prompt_path

        with pytest.raises(ValueError):
            safe_prompt_path('12345', '../escape')

        with pytest.raises(ValueError):
            safe_prompt_path('12345', '0', '../../etc')


# ---------------------------------------------------------------------------
# 7. safe_prompt_path — valid inputs
# ---------------------------------------------------------------------------

class TestSafePromptPathValid:
    """Verify normal prompt_id returns correct path."""

    def test_safe_prompt_path_valid(self):
        """Normal alphanumeric prompt_ids produce correct paths."""
        from helper import safe_prompt_path, PROMPTS_DIR

        # Single part
        path = safe_prompt_path('12345')
        assert path == os.path.join(PROMPTS_DIR, '12345.json')

        # Two parts (prompt_id + flow_id)
        path = safe_prompt_path('12345', '0')
        assert path == os.path.join(PROMPTS_DIR, '12345_0.json')

        # Three parts (prompt_id + flow_id + 'recipe')
        path = safe_prompt_path('12345', '0', 'recipe')
        assert path == os.path.join(PROMPTS_DIR, '12345_0_recipe.json')

        # Action recipe
        path = safe_prompt_path('12345', '0', '1')
        assert path == os.path.join(PROMPTS_DIR, '12345_0_1.json')

    def test_safe_prompt_path_hyphen_and_underscore(self):
        """Hyphens and underscores are accepted in path components."""
        from helper import safe_prompt_path, PROMPTS_DIR

        path = safe_prompt_path('my-prompt', 'flow_1')
        assert path == os.path.join(PROMPTS_DIR, 'my-prompt_flow_1.json')

    def test_safe_prompt_path_custom_extension(self):
        """Custom extension via ext= kwarg."""
        from helper import safe_prompt_path, PROMPTS_DIR

        path = safe_prompt_path('12345', ext='.txt')
        assert path == os.path.join(PROMPTS_DIR, '12345.txt')


# ---------------------------------------------------------------------------
# 8. Action to_dict — serialization test
# ---------------------------------------------------------------------------

class TestActionToDict:
    """Verify Action state can be serialized to a dict for persistence.

    The Action class does not have a built-in to_dict(). This test verifies
    that the Action's public attributes are serializable and round-trip
    through JSON, which is the pattern used by the recipe pipeline.
    """

    def test_action_to_dict(self):
        """Serialize Action attributes to dict and verify JSON round-trip."""
        from helper import Action

        raw_actions = [
            {'action_id': 1, 'action': 'Search web', 'tool': 'google_search'},
            {'action_id': 2, 'action': 'Summarize', 'tool': 'llm'},
            {'action_id': 3, 'action': 'Reply to user', 'tool': 'send_message_to_user'},
        ]
        action = Action(raw_actions)
        action.current_action = 2
        action.fallback = True
        action.recipe = True
        action.new_json = [{'step': 1, 'output': 'result A'}]

        # Serialize the public state
        d = {
            'actions': action.actions,
            'current_action': action.current_action,
            'fallback': action.fallback,
            'recipe': action.recipe,
            'new_json': action.new_json,
        }

        # JSON round-trip
        serialized = json.dumps(d)
        deserialized = json.loads(serialized)

        assert deserialized['current_action'] == 2
        assert deserialized['fallback'] is True
        assert deserialized['recipe'] is True
        assert len(deserialized['actions']) == 3
        assert deserialized['actions'][0]['tool'] == 'google_search'
        assert deserialized['new_json'][0]['output'] == 'result A'

    def test_action_empty_state_serializable(self):
        """An Action with default state serializes cleanly."""
        from helper import Action

        action = Action([])
        d = {
            'actions': action.actions,
            'current_action': action.current_action,
            'fallback': action.fallback,
            'recipe': action.recipe,
            'new_json': action.new_json,
        }

        serialized = json.dumps(d)
        deserialized = json.loads(serialized)

        assert deserialized['actions'] == []
        assert deserialized['current_action'] == 1
        assert deserialized['fallback'] is False


# ---------------------------------------------------------------------------
# 9. Validate_state_transition — direct coverage
# ---------------------------------------------------------------------------

class TestValidateStateTransition:
    """Direct tests for validate_state_transition()."""

    def setup_method(self):
        from lifecycle_hooks import action_states
        action_states.clear()

    def test_idempotent_same_state(self):
        """Setting the same state twice is a no-op (idempotent)."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
        )

        prompt = 'idempotent_user'
        aid = 1

        # Move to IN_PROGRESS
        set_action_state(prompt, aid, ActionState.IN_PROGRESS)
        # Same state again — should not raise
        set_action_state(prompt, aid, ActionState.IN_PROGRESS)
        assert get_action_state(prompt, aid) == ActionState.IN_PROGRESS

    def test_terminated_to_assigned_allowed(self):
        """TERMINATED -> ASSIGNED is allowed (action can be re-run)."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
            force_state_through_valid_path,
        )

        prompt = 'rerun_user'
        aid = 1

        # Walk to TERMINATED via valid path
        force_state_through_valid_path(prompt, aid, ActionState.TERMINATED, reason='test')

        # Verify we are TERMINATED (force_state walks through intermediates)
        # Depending on path availability, manually walk if needed
        # Let's just walk manually for certainty
        from lifecycle_hooks import action_states
        action_states.clear()

        for state in [
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED,
            ActionState.TERMINATED,
        ]:
            set_action_state(prompt, aid, state)

        assert get_action_state(prompt, aid) == ActionState.TERMINATED

        # Now TERMINATED -> ASSIGNED
        set_action_state(prompt, aid, ActionState.ASSIGNED)
        assert get_action_state(prompt, aid) == ActionState.ASSIGNED

    def test_preview_path(self):
        """ASSIGNED -> PREVIEW_PENDING -> PREVIEW_APPROVED -> IN_PROGRESS."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
        )

        prompt = 'preview_user'
        aid = 1

        set_action_state(prompt, aid, ActionState.PREVIEW_PENDING)
        assert get_action_state(prompt, aid) == ActionState.PREVIEW_PENDING

        set_action_state(prompt, aid, ActionState.PREVIEW_APPROVED)
        assert get_action_state(prompt, aid) == ActionState.PREVIEW_APPROVED

        set_action_state(prompt, aid, ActionState.IN_PROGRESS)
        assert get_action_state(prompt, aid) == ActionState.IN_PROGRESS


# ---------------------------------------------------------------------------
# 10. Thread safety of action_states
# ---------------------------------------------------------------------------

class TestStateMachineThreadSafety:
    """Verify concurrent state transitions don't corrupt global state."""

    def setup_method(self):
        from lifecycle_hooks import action_states
        action_states.clear()

    def test_concurrent_transitions(self):
        """Multiple threads transitioning different actions concurrently."""
        from lifecycle_hooks import (
            ActionState,
            set_action_state,
            get_action_state,
        )

        errors = []

        def walk_lifecycle(prompt, aid):
            try:
                for state in [
                    ActionState.IN_PROGRESS,
                    ActionState.STATUS_VERIFICATION_REQUESTED,
                    ActionState.COMPLETED,
                    ActionState.TERMINATED,
                ]:
                    set_action_state(prompt, aid, state, reason='thread test')
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(target=walk_lifecycle, args=(f'thread_user_{i}', 1))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

        # Verify all reached TERMINATED
        for i in range(10):
            assert get_action_state(f'thread_user_{i}', 1) == ActionState.TERMINATED
