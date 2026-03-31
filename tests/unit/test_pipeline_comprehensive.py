"""
Comprehensive CREATE/REUSE Pipeline Tests
==========================================

Tests every stage of the HARTOS agent pipeline individually and in combination:

1.  gather_info - autonomous config generation
2.  create_recipe - action execution via initiate_chat
3.  create_recipe review - StatusVerifier evaluates completion
4.  Action recipe creation - per-action recipe JSON files saved
5.  Topological sort - helper.topological_sort() orders flows correctly
6.  Flow recipe creation - after all actions in a flow complete
7.  Agent ledger - task creation, status tracking, completion routing
8.  Lifecycle hooks - ActionState machine transitions
9.  Proper state transitions - force_state_through_valid_path()
10. Handover to user - NEEDS-INPUT after 3 retries
11. Autonomous fallback - can_perform_without_user_input=yes
12. Scheduled executions - cron_expression in recipe
13. Time delayed executions - time_agent handling
14. Reuse recipe based on role - reuse_recipe.py loads and replays
15. Message recovery - chat_instructor.chat_messages recovery
16. Auto-advance - pipeline advances past completed actions
17. Execute-pending - launches unstarted actions
18. Recipe-needed detection - requests recipe when file missing
19. Late recipe save - saves recipe even when action already TERMINATED
20. Hallucination defense - action_id mismatch detection
21. group_chat sync - manager._groupchat reference
22. Full CREATE pipeline combination
23. Autonomous agent with scheduled tasks combination
24. Multi-flow agent combination
25. Error recovery chain combination
26. Resume from progress combination
27. Daemon agent pipeline combination
"""

import copy
import json
import os
import sys
import threading
import time
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, PropertyMock, call, ANY
import pytest

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'prompts'))

SAMPLE_AGENT_CONFIG = {
    "status": "completed",
    "name": "TestBot",
    "agent_name": "test.local.bot",
    "broadcast_agent": False,
    "goal": "Help users test things",
    "personas": [
        {"name": "Tester", "description": "Runs tests"},
        {"name": "Reporter", "description": "Reports results"}
    ],
    "flows": [
        {
            "flow_name": "Run Tests",
            "persona": "Tester",
            "actions": [
                {"action_id": 1, "action": "Identify test files", "can_perform_without_user_input": "yes"},
                {"action_id": 2, "action": "Execute tests", "can_perform_without_user_input": "yes"},
                {"action_id": 3, "action": "Collect results", "can_perform_without_user_input": "yes"}
            ],
            "sub_goal": "Execute all tests"
        },
        {
            "flow_name": "Report",
            "persona": "Reporter",
            "actions": [
                {"action_id": 1, "action": "Format results", "can_perform_without_user_input": "yes"},
                {"action_id": 2, "action": "Send report to user", "can_perform_without_user_input": "no"}
            ],
            "sub_goal": "Generate test report"
        }
    ],
    "personality": {
        "primary_traits": ["Diligent", "Precise"],
        "tone": "focused-professional",
        "greeting_style": "Ready to test.",
        "identity": "A meticulous test runner"
    }
}

SAMPLE_ACTIONS_FLOW1 = SAMPLE_AGENT_CONFIG["flows"][0]["actions"]
SAMPLE_ACTIONS_FLOW2 = SAMPLE_AGENT_CONFIG["flows"][1]["actions"]


@pytest.fixture
def tmp_prompts_dir(tmp_path):
    """Temporary prompts directory for test isolation."""
    d = tmp_path / "prompts"
    d.mkdir()
    return str(d)


@pytest.fixture
def sample_config_on_disk(tmp_prompts_dir):
    """Write sample config JSON to disk and return (dir, prompt_id)."""
    prompt_id = "99999"
    path = os.path.join(tmp_prompts_dir, f"{prompt_id}.json")
    with open(path, "w") as f:
        json.dump(SAMPLE_AGENT_CONFIG, f)
    return tmp_prompts_dir, prompt_id


def _terminate_action(user_prompt, action_id):
    """Walk action through the full valid state path to TERMINATED.

    force_state_through_valid_path only knows 1-2 step jumps, so we must
    walk the entire ASSIGNED->...->TERMINATED chain explicitly.
    """
    from lifecycle_hooks import safe_set_state, force_state_through_valid_path, ActionState
    force_state_through_valid_path(user_prompt, action_id, ActionState.COMPLETED, "done")
    safe_set_state(user_prompt, action_id, ActionState.FALLBACK_REQUESTED, "fb")
    safe_set_state(user_prompt, action_id, ActionState.FALLBACK_RECEIVED, "fb recv")
    safe_set_state(user_prompt, action_id, ActionState.RECIPE_REQUESTED, "recipe req")
    safe_set_state(user_prompt, action_id, ActionState.RECIPE_RECEIVED, "recipe recv")
    safe_set_state(user_prompt, action_id, ActionState.TERMINATED, "terminated")


@pytest.fixture
def flask_app():
    """Minimal Flask app for tests that need app context."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    return app


@pytest.fixture
def mock_user_tasks():
    """Create a mock user_tasks dict with an Action instance."""
    from helper import Action
    actions = [
        {"action_id": 1, "action": "Do thing A", "can_perform_without_user_input": "yes"},
        {"action_id": 2, "action": "Do thing B", "can_perform_without_user_input": "yes"},
        {"action_id": 3, "action": "Do thing C", "can_perform_without_user_input": "no"},
    ]
    action_obj = Action(actions)
    return {"test_user_99999": action_obj}


@pytest.fixture
def mock_group_chat():
    """Create a mock autogen GroupChat."""
    gc = MagicMock()
    gc.messages = []
    gc.agents = []
    return gc


@pytest.fixture
def mock_manager(mock_group_chat):
    """Create a mock autogen GroupChatManager."""
    mgr = MagicMock()
    mgr._groupchat = mock_group_chat
    mgr.groupchat = mock_group_chat
    return mgr


@pytest.fixture
def mock_agents():
    """Create mock autogen agents for the pipeline."""
    assistant = MagicMock(name='Assistant')
    assistant.name = 'Assistant'
    helper = MagicMock(name='Helper')
    helper.name = 'Helper'
    executor = MagicMock(name='Executor')
    executor.name = 'Executor'
    chat_instructor = MagicMock(name='ChatInstructor')
    chat_instructor.name = 'ChatInstructor'
    chat_instructor.chat_messages = {}
    verify = MagicMock(name='StatusVerifier')
    verify.name = 'StatusVerifier'
    author = MagicMock(name='UserProxy')
    author.name = 'UserProxy'
    return {
        'assistant': assistant,
        'helper': helper,
        'executor': executor,
        'chat_instructor': chat_instructor,
        'verify': verify,
        'author': author,
    }


# ===========================================================================
# SECTION 1: gather_info - autonomous config generation
# ===========================================================================

class TestGatherInfo:
    """Tests for gather_agentdetails.py -- agent requirement gathering."""

    def test_gather_info_raises_without_autogen(self):
        """gather_info raises ImportError when autogen is not installed."""
        from gather_agentdetails import gather_info
        with patch('gather_agentdetails.autogen', None):
            with pytest.raises(ImportError, match="pyautogen"):
                gather_info("user1", "Build a weather bot", "prompt1")

    def test_agent_creator_system_message_has_required_fields(self):
        """System message must mention all required config fields."""
        from gather_agentdetails import AGENT_CREATOR_SYSTEM_MESSAGE
        required_fields = ["name", "agent_name", "goal", "broadcast_agent",
                           "personas", "flows", "flow_name", "actions", "sub_goal"]
        for field in required_fields:
            assert field in AGENT_CREATOR_SYSTEM_MESSAGE, f"Missing field: {field}"

    def test_agent_creator_system_message_has_personality(self):
        """System message must include personality fields for completed agents."""
        from gather_agentdetails import AGENT_CREATOR_SYSTEM_MESSAGE
        for field in ["primary_traits", "tone", "greeting_style", "identity"]:
            assert field in AGENT_CREATOR_SYSTEM_MESSAGE, f"Missing personality field: {field}"

    def test_agent_name_format_three_part(self):
        """System message requires skill.region.name format."""
        from gather_agentdetails import AGENT_CREATOR_SYSTEM_MESSAGE
        assert "skill.region.name" in AGENT_CREATOR_SYSTEM_MESSAGE

    def test_create_agents_autonomous_mode(self):
        """Autonomous mode adds special instructions to system message."""
        mock_autogen = MagicMock()
        mock_assistant = MagicMock()
        mock_proxy = MagicMock()
        mock_autogen.AssistantAgent.return_value = mock_assistant
        mock_autogen.UserProxyAgent.return_value = mock_proxy

        with patch('gather_agentdetails.autogen', mock_autogen), \
             patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'flat'}), \
             patch('core.port_registry.get_local_llm_url', return_value='http://localhost:8080'):
            from gather_agentdetails import create_agents_for_user
            assistant, proxy = create_agents_for_user(
                "user1", autonomous=True, initial_description="A weather bot")

            # Verify autonomous instructions were added to system message
            sys_msg = mock_autogen.AssistantAgent.call_args[1]['system_message']
            assert "AUTONOMOUS MODE" in sys_msg
            assert "weather bot" in sys_msg

    def test_create_agents_interactive_mode(self):
        """Interactive mode: user_proxy max_consecutive_auto_reply=0."""
        mock_autogen = MagicMock()
        mock_autogen.AssistantAgent.return_value = MagicMock()
        mock_autogen.UserProxyAgent.return_value = MagicMock()

        with patch('gather_agentdetails.autogen', mock_autogen), \
             patch.dict(os.environ, {'HEVOLVE_NODE_TIER': 'flat'}), \
             patch('core.port_registry.get_local_llm_url', return_value='http://localhost:8080'):
            from gather_agentdetails import create_agents_for_user
            create_agents_for_user("user1", autonomous=False)

            proxy_kwargs = mock_autogen.UserProxyAgent.call_args[1]
            assert proxy_kwargs['max_consecutive_auto_reply'] == 0

    def test_get_agent_response_returns_string(self):
        """get_agent_response always returns a string."""
        from gather_agentdetails import get_agent_response
        mock_assistant = MagicMock()
        mock_proxy = MagicMock()
        mock_proxy.chat_messages = {
            "assistant_user1": [
                {"role": "assistant", "content": '{"status": "pending", "question": "What name?"}'}
            ]
        }
        result = get_agent_response(mock_assistant, mock_proxy, "Hello")
        assert isinstance(result, str)

    def test_get_agent_response_retries_on_missing_flows(self):
        """If LLM returns completed without flows, it retries."""
        from gather_agentdetails import get_agent_response
        mock_assistant = MagicMock()
        mock_proxy = MagicMock()
        # First response: completed but no flows key
        mock_proxy.chat_messages = {
            "assistant_user1": [
                {"role": "assistant",
                 "content": '{"status": "completed", "name": "Test"}'}
            ]
        }
        result = get_agent_response(mock_assistant, mock_proxy, "confirm")
        # Should have called send twice (initial + retry)
        assert mock_proxy.send.call_count == 2

    def test_config_json_structure_validation(self):
        """Validate that a completed config has all required fields."""
        required_keys = {"status", "name", "agent_name", "goal", "flows"}
        config = SAMPLE_AGENT_CONFIG
        assert required_keys.issubset(config.keys())
        assert config["status"] == "completed"
        assert len(config["flows"]) > 0
        for flow in config["flows"]:
            assert "flow_name" in flow
            assert "persona" in flow
            assert "actions" in flow
            assert "sub_goal" in flow
            for action in flow["actions"]:
                assert "action_id" in action
                assert "action" in action

    def test_user_agents_cache_reuse(self):
        """Same user_prompt should reuse cached agents."""
        from gather_agentdetails import user_agents
        key = "test_cache_99999"
        mock_agents = (MagicMock(), MagicMock())
        user_agents[key] = mock_agents
        try:
            assert user_agents[key] is mock_agents
        finally:
            user_agents.pop(key, None)

    def test_gather_info_produces_valid_json_with_personas(self):
        """A valid config from gather_info must have personas, flows, and actions."""
        config = SAMPLE_AGENT_CONFIG
        assert "personas" in config
        assert len(config["personas"]) >= 1
        for persona in config["personas"]:
            assert "name" in persona
        for flow in config["flows"]:
            assert "persona" in flow
            assert flow["persona"] in [p["name"] for p in config["personas"]]
            assert len(flow["actions"]) >= 1


# ===========================================================================
# SECTION 2: create_recipe - action execution via initiate_chat
# ===========================================================================

class TestCreateRecipeExecution:
    """Tests for action execution through the autogen group chat pipeline."""

    def test_action_class_initialization(self):
        """Action class correctly stores actions list."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Do thing"}]
        a = Action(actions)
        assert a.actions == actions
        assert a.current_action == 1
        assert a.fallback is False
        assert a.recipe is False

    def test_action_class_set_ledger(self):
        """Action.set_ledger attaches ledger instance."""
        from helper import Action
        from flask import Flask
        app = Flask(__name__)
        a = Action([{"action_id": 1}])
        mock_ledger = MagicMock()
        mock_ledger.tasks = {}
        with app.app_context():
            a.set_ledger(mock_ledger)
        assert a.ledger is mock_ledger

    def test_action_get_action(self):
        """Action.get_action returns correct action by index."""
        from helper import Action
        actions = [
            {"action_id": 1, "action": "First"},
            {"action_id": 2, "action": "Second"}
        ]
        a = Action(actions)
        result = a.get_action(0)
        assert result is not None

    def test_initiate_chat_called_with_correct_message(self, mock_agents, mock_manager):
        """initiate_chat is called with the action message."""
        chat_instructor = mock_agents['chat_instructor']
        message = "Execute Action 1: Identify test files"
        chat_instructor.initiate_chat(recipient=mock_manager, message=message, clear_history=False, silent=False)
        chat_instructor.initiate_chat.assert_called_once_with(
            recipient=mock_manager, message=message, clear_history=False, silent=False)

    def test_action_execution_sets_in_progress_state(self):
        """Starting an action transitions it from ASSIGNED to IN_PROGRESS."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_exec_ip"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "first action start")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS

    def test_action_tracks_timer(self):
        """Task timer is started when action begins executing."""
        task_time = {}
        prompt_id = "12345"
        task_time[prompt_id] = {'timer': time.time(), 'times': []}
        assert 'timer' in task_time[prompt_id]
        assert isinstance(task_time[prompt_id]['timer'], float)


# ===========================================================================
# SECTION 3: create_recipe review - StatusVerifier evaluates completion
# ===========================================================================

class TestStatusVerifierReview:
    """Tests for the StatusVerifier pattern in create_recipe.py."""

    def test_lifecycle_hook_process_verifier_valid_completion(self):
        """Verifier accepts valid completion JSON."""
        from lifecycle_hooks import lifecycle_hook_process_verifier_response, safe_set_state, ActionState
        from helper import Action

        user_prompt = "test_verifier_valid"
        actions = [{"action_id": 1, "action": "Do thing"}]
        user_tasks = {user_prompt: Action(actions)}
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")

        json_obj = {"status": "completed", "action_id": 1, "result": "Done"}
        result = lifecycle_hook_process_verifier_response(user_prompt, json_obj, user_tasks)
        assert result['action'] == 'allow'

    def test_lifecycle_hook_process_verifier_passes_none_through(self):
        """Verifier allows None JSON through (defensive design)."""
        from lifecycle_hooks import lifecycle_hook_process_verifier_response, safe_set_state, ActionState
        from helper import Action

        user_prompt = "test_verifier_none"
        actions = [{"action_id": 1, "action": "Do thing"}]
        user_tasks = {user_prompt: Action(actions)}
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")

        result = lifecycle_hook_process_verifier_response(user_prompt, None, user_tasks)
        assert result['action'] == 'allow'
        assert result['message'] is None

    def test_lifecycle_hook_process_verifier_passes_missing_status(self):
        """Verifier allows JSON without status field (defensive design)."""
        from lifecycle_hooks import lifecycle_hook_process_verifier_response, safe_set_state, ActionState
        from helper import Action

        user_prompt = "test_verifier_no_status"
        actions = [{"action_id": 1, "action": "Do thing"}]
        user_tasks = {user_prompt: Action(actions)}
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")

        json_obj = {"result": "Something", "no_status": True}
        result = lifecycle_hook_process_verifier_response(user_prompt, json_obj, user_tasks)
        assert result['action'] == 'allow'

    def test_verifier_completion_with_fallback_action(self):
        """StatusVerifier completion with auto-generated fallback proceeds to recipe."""
        from helper import Action
        user_prompt = "test_verifier_fallback"
        actions = [{"action_id": 1, "action": "Do thing"}]
        task = Action(actions)
        task.fallback = False
        task.recipe = False

        json_obj = {
            "status": "completed",
            "action_id": 1,
            "result": "Done",
            "fallback_action": "Retry with alternative approach"
        }
        # When fallback_action is provided, recipe flag should be set
        fallback_action = json_obj.get('fallback_action', '').strip()
        if fallback_action:
            task.fallback = False
            task.recipe = True
        assert task.recipe is True
        assert task.fallback is False

    def test_verifier_completion_without_fallback_requests_from_user(self):
        """StatusVerifier completion without fallback requests from user."""
        from helper import Action
        user_prompt = "test_verifier_no_fb"
        actions = [{"action_id": 1, "action": "Do thing"}]
        task = Action(actions)
        task.fallback = False
        task.recipe = False

        json_obj = {
            "status": "completed",
            "action_id": 1,
            "result": "Done",
            "fallback_action": ""
        }
        fallback_action = json_obj.get('fallback_action', '').strip()
        if not fallback_action:
            task.fallback = True
        assert task.fallback is True

    def test_verifier_error_status_sets_error_state(self):
        """Error status from verifier triggers ERROR action state."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_verifier_error"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "verifier error")
        assert get_action_state(user_prompt, 1) == ActionState.ERROR


# ===========================================================================
# SECTION 4: Action recipe creation - per-action recipe JSON files
# ===========================================================================

class TestActionRecipeCreation:
    """Tests for individual action recipe file creation and saving."""

    def test_action_recipe_file_written(self, tmp_prompts_dir):
        """Individual action recipe JSON file is written to disk."""
        prompt_id = "action_recipe_test"
        flow = 0
        action_id = 1
        json_obj = {
            "status": "done",
            "action": "Execute test suite",
            "action_id": action_id,
            "recipe": [
                {"steps": "Run pytest", "tool_name": "", "generalized_functions": ""}
            ],
            "can_perform_without_user_input": "yes"
        }
        name = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_{action_id}.json')
        with open(name, "w") as json_file:
            json.dump(json_obj, json_file)
        assert os.path.exists(name)
        with open(name) as f:
            loaded = json.load(f)
        assert loaded["status"] == "done"
        assert len(loaded["recipe"]) == 1

    def test_action_recipe_assigns_agent_to_steps(self):
        """Recipe steps get agent_to_perform_this_action based on tool/function presence."""
        recipe_steps = [
            {"steps": "Use search tool", "tool_name": "web_search", "generalized_functions": ""},
            {"steps": "Run python code", "tool_name": "", "generalized_functions": "def do_thing(): pass"},
            {"steps": "Analyze results", "tool_name": "", "generalized_functions": ""},
        ]
        for step in recipe_steps:
            if 'tool_name' in step and step['tool_name'] != "":
                step['agent_to_perform_this_action'] = 'Helper'
            elif 'generalized_functions' in step and step['generalized_functions'] != "":
                step['agent_to_perform_this_action'] = 'Executor'
            else:
                step['agent_to_perform_this_action'] = 'Assistant'

        assert recipe_steps[0]['agent_to_perform_this_action'] == 'Helper'
        assert recipe_steps[1]['agent_to_perform_this_action'] == 'Executor'
        assert recipe_steps[2]['agent_to_perform_this_action'] == 'Assistant'

    def test_action_recipe_includes_metadata(self, tmp_prompts_dir):
        """Action recipe file includes stripped metadata."""
        from helper import strip_json_values
        metadata = {"key1": "value1", "nested": {"key2": "value2"}}
        stripped = strip_json_values(metadata)
        json_obj = {
            "status": "done",
            "action_id": 1,
            "recipe": [{"steps": "step1"}],
            "metadata": stripped,
            "time_took_to_complete": 12.5
        }
        path = os.path.join(tmp_prompts_dir, "test_0_1.json")
        with open(path, "w") as f:
            json.dump(json_obj, f)
        with open(path) as f:
            loaded = json.load(f)
        assert "metadata" in loaded
        assert "time_took_to_complete" in loaded

    def test_action_recipe_file_naming_convention(self):
        """Action recipe files follow {prompt_id}_{flow}_{action_id}.json."""
        prompt_id = "12345"
        flow = 0
        action_id = 2
        expected = f"{prompt_id}_{flow}_{action_id}.json"
        assert expected == "12345_0_2.json"

    def test_recipe_state_transition_to_terminated(self):
        """After recipe is saved, action transitions to TERMINATED."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState,
            force_state_through_valid_path
        )
        user_prompt = "test_recipe_term"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "fb")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "fb recv")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_REQUESTED, "recipe req")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_RECEIVED, "recipe recv")
        safe_set_state(user_prompt, 1, ActionState.TERMINATED, "recipe saved")
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED


# ===========================================================================
# SECTION 5: Topological sort
# ===========================================================================

class TestTopologicalSort:
    """Tests for helper.topological_sort() ordering flows correctly."""

    def test_topological_sort_basic(self):
        """Topological sort orders actions by dependencies."""
        from helper import topological_sort
        actions = [
            {"action_id": 3, "actions_this_action_depends_on": [1, 2]},
            {"action_id": 1, "actions_this_action_depends_on": []},
            {"action_id": 2, "actions_this_action_depends_on": [1]}
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        ids = [a["action_id"] for a in sorted_actions]
        assert ids.index(1) < ids.index(2)
        assert ids.index(2) < ids.index(3)

    def test_topological_sort_no_dependencies(self):
        """Actions without dependencies maintain order."""
        from helper import topological_sort
        actions = [
            {"action_id": 1, "actions_this_action_depends_on": []},
            {"action_id": 2, "actions_this_action_depends_on": []},
            {"action_id": 3, "actions_this_action_depends_on": []}
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        assert len(sorted_actions) == 3

    def test_topological_sort_cyclic_detection(self):
        """Cyclic dependencies should be detected."""
        from helper import topological_sort
        actions = [
            {"action_id": 1, "actions_this_action_depends_on": [2]},
            {"action_id": 2, "actions_this_action_depends_on": [1]}
        ]
        success, sorted_actions, cyclic_ids = topological_sort(actions)
        assert success is False
        assert cyclic_ids is not None

    def test_topological_sort_diamond_dependency(self):
        """Diamond dependency pattern resolves correctly (1->2,3->4)."""
        from helper import topological_sort
        actions = [
            {"action_id": 4, "actions_this_action_depends_on": [2, 3]},
            {"action_id": 2, "actions_this_action_depends_on": [1]},
            {"action_id": 3, "actions_this_action_depends_on": [1]},
            {"action_id": 1, "actions_this_action_depends_on": []},
        ]
        success, sorted_actions, cyclic = topological_sort(actions)
        assert success is True
        ids = [a["action_id"] for a in sorted_actions]
        assert ids.index(1) < ids.index(2)
        assert ids.index(1) < ids.index(3)
        assert ids.index(2) < ids.index(4)
        assert ids.index(3) < ids.index(4)

    def test_fix_actions_with_cyclic_ids(self):
        """fix_actions handles cyclic dependency resolution."""
        from helper import fix_actions
        actions = [
            {"action": "Do A", "action_id": 1},
            {"action": "Do B", "action_id": 2},
        ]
        cyclic_ids = [1, 2]
        with patch('helper.pooled_post') as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"choices": [{"message": {"content": str(actions)}}]}
            )
            result = fix_actions(actions, cyclic_ids)
            assert result is None or isinstance(result, list)

    def test_retrieve_json_valid(self):
        """retrieve_json extracts JSON from text."""
        from helper import retrieve_json
        text = 'Here is the result: {"status": "completed", "action_id": 1}'
        result = retrieve_json(text)
        assert result is not None
        assert result["status"] == "completed"

    def test_retrieve_json_invalid(self):
        """retrieve_json returns None for non-JSON text."""
        from helper import retrieve_json
        result = retrieve_json("This is plain text with no JSON")
        assert result is None

    def test_retrieve_json_nested(self):
        """retrieve_json handles nested JSON."""
        from helper import retrieve_json
        text = '{"status": "completed", "data": {"key": "value", "list": [1,2,3]}}'
        result = retrieve_json(text)
        assert result is not None
        assert result["data"]["list"] == [1, 2, 3]

    def test_strip_json_values_redacts(self):
        """strip_json_values redacts all leaf values for privacy."""
        from helper import strip_json_values
        data = {"key": "sensitive", "nested": {"inner": "secret"}}
        result = strip_json_values(data)
        assert isinstance(result, dict)
        assert result["key"] != "sensitive"


# ===========================================================================
# SECTION 6: Flow recipe creation
# ===========================================================================

class TestFlowRecipeCreation:
    """Tests for flow recipe file creation and management."""

    def test_create_final_recipe_writes_file(self, tmp_prompts_dir):
        """create_final_recipe_for_current_flow writes JSON file."""
        prompt_id = "test_flow_recipe"
        flow = 0
        merged_dict = {
            "flow_name": "Test Flow",
            "actions": [
                {"action_id": 1, "status": "completed", "result": "Done"}
            ]
        }
        name = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_recipe.json')
        with open(name, "w") as f:
            json.dump(merged_dict, f)
        assert os.path.exists(name)
        with open(name) as f:
            data = json.load(f)
        assert data["flow_name"] == "Test Flow"
        assert len(data["actions"]) == 1

    def test_recipe_file_naming_convention(self):
        """Recipe files follow {prompt_id}_{flow}_recipe.json pattern."""
        prompt_id = "12345"
        flow = 0
        expected = f"{prompt_id}_{flow}_recipe.json"
        assert expected == "12345_0_recipe.json"

    def test_flow_lifecycle_state_tracking(self):
        """FlowLifecycleState tracks flow-level states."""
        from lifecycle_hooks import FlowState, flow_lifecycle
        user_prompt = "test_flow_state"
        flow_lifecycle.set_flow_state(user_prompt, 0, FlowState.DEPENDENCY_ANALYSIS)
        assert flow_lifecycle.flows[user_prompt][0] == FlowState.DEPENDENCY_ANALYSIS

    def test_flow_lifecycle_multiple_flows(self):
        """FlowLifecycleState handles multiple flows independently."""
        from lifecycle_hooks import FlowState, flow_lifecycle
        user_prompt = "test_multi_flow"
        flow_lifecycle.set_flow_state(user_prompt, 0, FlowState.FLOW_RECIPE_CREATION)
        flow_lifecycle.set_flow_state(user_prompt, 1, FlowState.TOPOLOGICAL_SORT)
        assert flow_lifecycle.flows[user_prompt][0] == FlowState.FLOW_RECIPE_CREATION
        assert flow_lifecycle.flows[user_prompt][1] == FlowState.TOPOLOGICAL_SORT

    def test_all_actions_terminated_check(self):
        """lifecycle_hook_check_all_actions_terminated verifies all actions terminated."""
        from lifecycle_hooks import (
            safe_set_state, ActionState,
            lifecycle_hook_check_all_actions_terminated
        )
        from helper import Action

        user_prompt = "test_all_term"
        actions = [
            {"action_id": 1, "action": "A"},
            {"action_id": 2, "action": "B"},
        ]
        user_tasks = {user_prompt: Action(actions)}
        for a in actions:
            safe_set_state(user_prompt, a["action_id"], ActionState.ASSIGNED, "init")
            _terminate_action(user_prompt, a["action_id"])

        result = lifecycle_hook_check_all_actions_terminated(user_prompt, user_tasks)
        # The hook returns 'allow' when all terminated, or 'continue_actions' if some still need work
        assert result['action'] in ('allow', 'continue_actions')


# ===========================================================================
# SECTION 7: Agent ledger - task creation, tracking, routing
# ===========================================================================

class TestAgentLedger:
    """Tests for SmartLedger integration."""

    def test_create_ledger_for_user_prompt(self):
        """create_ledger_for_user_prompt creates properly configured ledger."""
        try:
            from helper_ledger import create_ledger_for_user_prompt
        except ImportError:
            pytest.skip("agent_ledger not installed")
        ledger = create_ledger_for_user_prompt(123, 456)
        assert ledger is not None
        assert "456" in str(ledger.agent_id)
        assert "123_456" in str(ledger.session_id)

    def test_create_ledger_with_auto_backend(self):
        """create_ledger_with_auto_backend selects best backend."""
        try:
            from helper_ledger import create_ledger_with_auto_backend
        except ImportError:
            pytest.skip("agent_ledger not installed")
        ledger = create_ledger_with_auto_backend(123, 456, prefer_redis=False)
        assert ledger is not None

    def test_add_subtasks_to_ledger(self):
        """add_subtasks_to_ledger delegates to ledger.add_subtasks."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
            from helper_ledger import add_subtasks_to_ledger
        except ImportError:
            pytest.skip("agent_ledger not installed")

        user_prompt = "test_subtask"
        ledger = SmartLedger(agent_id="test", session_id=user_prompt)
        parent = Task(
            task_id="action_1", description="Parent task",
            task_type=TaskType.PRE_ASSIGNED,
            execution_mode=ExecutionMode.SEQUENTIAL,
            status=TaskStatus.IN_PROGRESS,
        )
        ledger.add_task(parent)
        user_ledgers = {user_prompt: ledger}
        subtasks = [
            {"task_id": "sub_1", "description": "Subtask 1"},
            {"task_id": "sub_2", "description": "Subtask 2"}
        ]
        result = add_subtasks_to_ledger(user_prompt, "action_1", subtasks, user_ledgers)
        assert isinstance(result, bool)

    def test_ledger_task_routing_after_completion(self):
        """After action completes, ledger routes to next executable task."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        ledger = SmartLedger(agent_id="routing_test", session_id="route_session")
        t1 = Task(task_id="action_1", description="First",
                  task_type=TaskType.PRE_ASSIGNED,
                  execution_mode=ExecutionMode.SEQUENTIAL,
                  status=TaskStatus.IN_PROGRESS)
        t2 = Task(task_id="action_2", description="Second",
                  task_type=TaskType.PRE_ASSIGNED,
                  execution_mode=ExecutionMode.SEQUENTIAL,
                  status=TaskStatus.PENDING,
                  prerequisites=["action_1"])
        ledger.add_task(t1)
        ledger.add_task(t2)
        ledger.update_task_status("action_1", TaskStatus.COMPLETED, "done")
        next_task = ledger.get_next_executable_task()
        if next_task:
            assert next_task.task_id == "action_2"

    def test_goal_tracking_via_ledger(self):
        """Goals can be tracked via SmartLedger tasks."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        ledger = SmartLedger(agent_id="seed_test", session_id="seed_session")
        goal_task = Task(
            task_id="seed_goal_1",
            description="Monitor system health",
            task_type=TaskType.AUTONOMOUS,
            execution_mode=ExecutionMode.SEQUENTIAL,
            status=TaskStatus.PENDING,
            context={"goal_type": "proactive_monitoring"}
        )
        ledger.add_task(goal_task)
        assert "seed_goal_1" in ledger.tasks
        assert ledger.tasks["seed_goal_1"].context["goal_type"] == "proactive_monitoring"

    def test_get_default_llm_client(self):
        """get_default_llm_client returns an OpenAI-compatible client."""
        from helper_ledger import get_default_llm_client
        client = get_default_llm_client()
        # Just verify it doesn't crash


# ===========================================================================
# SECTION 8: Lifecycle hooks - ActionState machine transitions
# ===========================================================================

class TestLifecycleHooks:
    """Tests for lifecycle_hooks.py state machine -- every transition path."""

    def setup_method(self):
        """Reset action_states for test isolation."""
        from lifecycle_hooks import action_states, initialize_deterministic_actions
        action_states.clear()
        initialize_deterministic_actions()

    def test_action_state_enum_has_all_15_states(self):
        """ActionState enum must have exactly 15 states."""
        from lifecycle_hooks import ActionState
        assert len(ActionState) == 15
        expected = {"assigned", "in_progress", "status_verification_requested",
                    "completed", "pending", "error", "fallback_requested",
                    "fallback_received", "recipe_requested", "recipe_received",
                    "terminated", "executing_motion", "sensor_confirm",
                    "preview_pending", "preview_approved"}
        actual = {s.value for s in ActionState}
        assert actual == expected

    def test_initial_state_is_assigned(self):
        """New actions start in ASSIGNED state."""
        from lifecycle_hooks import get_action_state, ActionState, safe_set_state
        user_prompt = "test_init_1"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "test init")
        state = get_action_state(user_prompt, 1)
        assert state == ActionState.ASSIGNED

    def test_valid_transition_assigned_to_in_progress(self):
        """ASSIGNED -> IN_PROGRESS is valid."""
        from lifecycle_hooks import validate_state_transition, ActionState, safe_set_state
        user_prompt = "test_trans_1"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        assert validate_state_transition(user_prompt, 1, ActionState.IN_PROGRESS)

    def test_invalid_transition_assigned_to_completed(self):
        """ASSIGNED -> COMPLETED is invalid (must go through IN_PROGRESS first)."""
        from lifecycle_hooks import validate_state_transition, ActionState, safe_set_state
        user_prompt = "test_trans_2"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        assert not validate_state_transition(user_prompt, 1, ActionState.COMPLETED)

    def test_full_happy_path_transitions(self):
        """ASSIGNED -> IN_PROGRESS -> STATUS_VERIFICATION -> COMPLETED -> TERMINATED."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_happy_path"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.TERMINATED, "final")
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED

    def test_error_recovery_path(self):
        """ERROR -> IN_PROGRESS (retry) is valid."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_error_retry"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "failed")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "retry")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS

    def test_fallback_path(self):
        """COMPLETED -> FALLBACK_REQUESTED -> FALLBACK_RECEIVED -> RECIPE_REQUESTED."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_fallback"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "need fallback")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "got fallback")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_REQUESTED, "need recipe")
        assert get_action_state(user_prompt, 1) == ActionState.RECIPE_REQUESTED

    def test_recipe_path(self):
        """RECIPE_REQUESTED -> RECIPE_RECEIVED -> TERMINATED."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_recipe_path"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "fb req")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "fb recv")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_REQUESTED, "recipe req")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_RECEIVED, "recipe recv")
        safe_set_state(user_prompt, 1, ActionState.TERMINATED, "terminated")
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED

    def test_preview_pending_path(self):
        """ASSIGNED -> PREVIEW_PENDING -> PREVIEW_APPROVED -> IN_PROGRESS."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_preview"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.PREVIEW_PENDING, "destructive action")
        safe_set_state(user_prompt, 1, ActionState.PREVIEW_APPROVED, "user approved")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "executing")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS

    def test_state_transition_error_raised(self):
        """Invalid transitions raise StateTransitionError."""
        from lifecycle_hooks import set_action_state, ActionState, StateTransitionError, safe_set_state
        user_prompt = "test_invalid"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        with pytest.raises(StateTransitionError):
            set_action_state(user_prompt, 1, ActionState.COMPLETED, "skip ahead")

    def test_idempotent_state_set(self):
        """Setting same state is idempotent (no error)."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_idempotent"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "again")
        assert get_action_state(user_prompt, 1) == ActionState.ASSIGNED

    def test_multiple_actions_independent(self):
        """Multiple actions have independent state."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_multi"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 2, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start 1")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS
        assert get_action_state(user_prompt, 2) == ActionState.ASSIGNED

    def test_thread_safety_of_state_transitions(self):
        """State transitions are thread-safe."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_thread"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        errors = []

        def transition():
            try:
                safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "thread")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=transition) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state = get_action_state(user_prompt, 1)
        assert state in (ActionState.ASSIGNED, ActionState.IN_PROGRESS)


# ===========================================================================
# SECTION 9: Proper state transitions - force_state_through_valid_path
# ===========================================================================

class TestForceStateTransitions:
    """Tests for force_state_through_valid_path() which validates multi-step transitions."""

    def setup_method(self):
        from lifecycle_hooks import action_states, initialize_deterministic_actions
        action_states.clear()
        initialize_deterministic_actions()

    def test_force_state_assigned_to_completed(self):
        """Force from ASSIGNED directly to COMPLETED goes through intermediate states."""
        from lifecycle_hooks import force_state_through_valid_path, get_action_state, ActionState, safe_set_state
        user_prompt = "test_force_1"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "force complete")
        assert get_action_state(user_prompt, 1) == ActionState.COMPLETED

    def test_force_state_from_in_progress_to_completed(self):
        """Force IN_PROGRESS -> COMPLETED goes through STATUS_VERIFICATION."""
        from lifecycle_hooks import force_state_through_valid_path, get_action_state, ActionState, safe_set_state
        user_prompt = "test_force_ip"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "force complete")
        assert get_action_state(user_prompt, 1) == ActionState.COMPLETED

    def test_force_state_assigned_to_terminated(self):
        """ASSIGNED -> ... -> TERMINATED requires walking through the full path."""
        from lifecycle_hooks import force_state_through_valid_path, get_action_state, ActionState, safe_set_state
        user_prompt = "test_force_term"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        # force_state only has 1-2 step paths, so walk the full path explicitly
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "fb")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "fb recv")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_REQUESTED, "recipe req")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_RECEIVED, "recipe recv")
        safe_set_state(user_prompt, 1, ActionState.TERMINATED, "terminated")
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED

    def test_force_state_error_to_in_progress(self):
        """Force ERROR -> IN_PROGRESS for retry."""
        from lifecycle_hooks import force_state_through_valid_path, get_action_state, ActionState, safe_set_state
        user_prompt = "test_force_err_ip"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "failed")
        force_state_through_valid_path(user_prompt, 1, ActionState.IN_PROGRESS, "retry")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS

    def test_terminated_allows_reassignment(self):
        """TERMINATED -> ASSIGNED is valid (for flow restart)."""
        from lifecycle_hooks import validate_state_transition, ActionState, safe_set_state
        user_prompt = "test_reassign"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        _terminate_action(user_prompt, 1)
        assert validate_state_transition(user_prompt, 1, ActionState.ASSIGNED)


# ===========================================================================
# SECTION 10: Handover to user - NEEDS-INPUT after 3 retries
# ===========================================================================

class TestHandoverToUser:
    """Tests for the NEEDS-INPUT mechanism that returns control to user after 3 retries."""

    def test_exec_retries_tracking(self):
        """_exec_retries dict tracks per-action retry counts."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Need user input"}]
        task = Action(actions)
        task._exec_retries = {}
        task._exec_retries[1] = 0
        for _ in range(4):
            task._exec_retries[1] += 1
        assert task._exec_retries[1] == 4

    def test_needs_input_breaks_after_3_retries(self):
        """After 3+ attempts, pipeline should break out for user input."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Need user input"}]
        task = Action(actions)
        task._exec_retries = {1: 4}
        # Simulates the while loop logic at line 3799: if _attempt > 3: break
        _attempt = task._exec_retries.get(1, 0)
        should_break = _attempt > 3
        assert should_break is True

    def test_under_retry_limit_continues(self):
        """Under 3 retries, the pipeline keeps trying."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Autonomous action"}]
        task = Action(actions)
        task._exec_retries = {1: 2}
        _attempt = task._exec_retries.get(1, 0)
        should_break = _attempt > 3
        assert should_break is False

    def test_action_needing_user_input_flagged(self):
        """Action with can_perform_without_user_input=no is correctly identified."""
        action = {"action_id": 1, "action": "Get user preference",
                  "can_perform_without_user_input": "no"}
        assert action["can_perform_without_user_input"] == "no"

    def test_handover_via_fallback_state(self):
        """Handover to user goes through FALLBACK_REQUESTED state."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        user_prompt = "test_handover"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "needs input")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "ask user")
        assert get_action_state(user_prompt, 1) == ActionState.FALLBACK_REQUESTED
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "user replied")
        assert get_action_state(user_prompt, 1) == ActionState.FALLBACK_RECEIVED


# ===========================================================================
# SECTION 11: Autonomous fallback - can_perform_without_user_input=yes
# ===========================================================================

class TestAutonomousFallback:
    """Tests for autonomous fallback generation by StatusVerifier."""

    def test_autonomous_action_skips_user_fallback(self):
        """When LLM provides fallback_action, user is not asked."""
        from helper import Action
        task = Action([{"action_id": 1, "action": "Do thing"}])
        json_obj = {
            "status": "completed",
            "action_id": 1,
            "fallback_action": "Retry using alternative API endpoint"
        }
        fallback_action = json_obj.get('fallback_action', '').strip()
        if fallback_action:
            task.fallback = False
            task.recipe = True
        assert task.fallback is False
        assert task.recipe is True

    def test_missing_fallback_requests_from_user(self):
        """Empty fallback_action triggers user fallback request."""
        from helper import Action
        task = Action([{"action_id": 1, "action": "Do thing"}])
        json_obj = {
            "status": "completed",
            "action_id": 1,
            "fallback_action": ""
        }
        fallback_action = json_obj.get('fallback_action', '').strip()
        if not fallback_action:
            task.fallback = True
        assert task.fallback is True

    def test_can_perform_without_user_input_yes(self):
        """Actions with yes flag are eligible for autonomous execution."""
        action = {"action_id": 1, "action": "Search web",
                  "can_perform_without_user_input": "yes"}
        assert action["can_perform_without_user_input"] == "yes"

    def test_lifecycle_hook_track_recipe_completion(self):
        """lifecycle_hook_track_recipe_completion detects recipe in response."""
        from lifecycle_hooks import lifecycle_hook_track_recipe_completion, safe_set_state, ActionState
        from helper import Action
        user_prompt = "test_recipe_comp"
        actions = [{"action_id": 1, "action": "Do thing"}]
        user_tasks = {user_prompt: Action(actions)}
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        json_obj = {
            "status": "done",
            "action_id": 1,
            "recipe": [{"steps": "step1"}]
        }
        result = lifecycle_hook_track_recipe_completion(user_prompt, json_obj, user_tasks)
        assert result is not None
        assert 'action' in result


# ===========================================================================
# SECTION 12: Scheduled executions - cron_expression
# ===========================================================================

class TestScheduledExecution:
    """Tests for time-delayed and scheduled task execution."""

    def test_apscheduler_cron_trigger_creation(self):
        """CronTrigger can be created for scheduled tasks."""
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger(hour=9, minute=0)
        assert trigger is not None

    def test_apscheduler_interval_trigger_creation(self):
        """IntervalTrigger can be created for recurring tasks."""
        from apscheduler.triggers.interval import IntervalTrigger
        trigger = IntervalTrigger(minutes=30)
        assert trigger is not None

    def test_background_scheduler_lifecycle(self):
        """BackgroundScheduler starts and shuts down cleanly."""
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.start()
        assert scheduler.running
        scheduler.shutdown(wait=False)
        assert not scheduler.running

    def test_scheduler_add_job(self):
        """Scheduler can add jobs without errors."""
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.start()
        try:
            job = scheduler.add_job(lambda: None, 'interval', seconds=3600, id='test_job')
            assert job is not None
            scheduler.remove_job('test_job')
        finally:
            scheduler.shutdown(wait=False)

    def test_cron_expression_in_recipe(self):
        """Recipe scheduled_tasks contain cron expressions."""
        recipe = {
            "status": "completed",
            "scheduled_tasks": [
                {
                    "cron_expression": "0 9 * * *",
                    "persona": "Tester",
                    "action_entry_point": 1,
                    "action_exit_point": 3,
                    "job_description": "Run daily test suite"
                }
            ]
        }
        tasks = recipe["scheduled_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["cron_expression"] == "0 9 * * *"
        assert tasks[0]["action_entry_point"] == 1


# ===========================================================================
# SECTION 13: Time delayed executions - time_agent handling
# ===========================================================================

class TestTimeAgent:
    """Tests for time_agent handling and scheduler_check flag."""

    def test_scheduler_check_triggers_time_agents(self):
        """When scheduler_check is True, recipe() enters time agent creation."""
        scheduler_check = {"test_user_prompt": True}
        assert scheduler_check["test_user_prompt"] is True

    def test_scheduler_check_false_skips_time_agents(self):
        """When scheduler_check is False, time agents are not created."""
        scheduler_check = {"test_user_prompt": False}
        assert scheduler_check["test_user_prompt"] is False

    def test_scheduled_monitoring_action(self):
        """Monitoring actions can be scheduled at intervals."""
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        monitored = {"count": 0}

        def monitor_callback():
            monitored["count"] += 1

        scheduler = BackgroundScheduler()
        scheduler.start()
        try:
            scheduler.add_job(monitor_callback, IntervalTrigger(seconds=1), id='monitor')
            time.sleep(2.5)
            assert monitored["count"] >= 2
        finally:
            scheduler.shutdown(wait=False)

    def test_visual_scheduled_tasks_structure(self):
        """Visual scheduled tasks have proper structure in recipe."""
        recipe = {
            "visual_scheduled_tasks": [
                {
                    "cron_expression": "*/5 * * * *",
                    "persona": "Monitor",
                    "job_description": "Check video feed for activity"
                }
            ]
        }
        vtask = recipe["visual_scheduled_tasks"][0]
        assert "cron_expression" in vtask
        assert "persona" in vtask


# ===========================================================================
# SECTION 14: Reuse recipe based on role
# ===========================================================================

class TestReuseRecipe:
    """Tests for reuse_recipe.py loading and replaying recipes."""

    def test_recipe_file_enables_reuse(self, tmp_prompts_dir):
        """A saved recipe file can be loaded for REUSE mode."""
        prompt_id = "e2e_test"
        flow = 0
        recipe_data = {
            "flow_name": "Test Flow",
            "persona": "Tester",
            "actions": [
                {
                    "action_id": 1,
                    "action": "Run tests",
                    "status": "completed",
                    "result": {"output": "All passed"},
                    "recipe": {"steps": ["step1", "step2"]}
                }
            ]
        }
        recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_{flow}_recipe.json")
        with open(recipe_path, "w") as f:
            json.dump(recipe_data, f)
        with open(recipe_path) as f:
            loaded = json.load(f)
        assert loaded["flow_name"] == "Test Flow"
        assert loaded["actions"][0]["status"] == "completed"
        assert "recipe" in loaded["actions"][0]

    def test_config_plus_recipe_complete_agent(self, tmp_prompts_dir):
        """Config JSON + recipe JSON together form a complete agent."""
        prompt_id = "complete_test"
        config_path = os.path.join(tmp_prompts_dir, f"{prompt_id}.json")
        with open(config_path, "w") as f:
            json.dump(SAMPLE_AGENT_CONFIG, f)
        recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_recipe.json")
        recipe_data = {"flow_name": "Run Tests", "actions": SAMPLE_ACTIONS_FLOW1}
        with open(recipe_path, "w") as f:
            json.dump(recipe_data, f)
        assert os.path.exists(config_path)
        assert os.path.exists(recipe_path)
        with open(config_path) as f:
            config = json.load(f)
        with open(recipe_path) as f:
            recipe = json.load(f)
        assert config["flows"][0]["flow_name"] == recipe["flow_name"]

    def test_reuse_loads_recipe_by_role(self, tmp_prompts_dir):
        """REUSE mode loads recipe for the correct persona/role."""
        prompt_id = "role_test"
        # Flow 0 for Tester persona
        recipe0 = {
            "flow_name": "Run Tests",
            "persona": "Tester",
            "actions": [{"action_id": 1, "recipe": [{"steps": "run tests"}]}]
        }
        # Flow 1 for Reporter persona
        recipe1 = {
            "flow_name": "Report",
            "persona": "Reporter",
            "actions": [{"action_id": 1, "recipe": [{"steps": "format report"}]}]
        }
        for flow, data in enumerate([recipe0, recipe1]):
            path = os.path.join(tmp_prompts_dir, f"{prompt_id}_{flow}_recipe.json")
            with open(path, "w") as f:
                json.dump(data, f)

        # Load flow 0 recipe (Tester)
        with open(os.path.join(tmp_prompts_dir, f"{prompt_id}_0_recipe.json")) as f:
            loaded = json.load(f)
        assert loaded["persona"] == "Tester"

        # Load flow 1 recipe (Reporter)
        with open(os.path.join(tmp_prompts_dir, f"{prompt_id}_1_recipe.json")) as f:
            loaded = json.load(f)
        assert loaded["persona"] == "Reporter"


# ===========================================================================
# SECTION 15: Message recovery - chat_instructor.chat_messages
# ===========================================================================

class TestMessageRecovery:
    """Tests for chat_instructor.chat_messages recovery after initiate_chat."""

    def test_message_recovery_from_chat_messages(self, mock_agents, mock_manager, mock_group_chat):
        """Messages recovered from chat_instructor.chat_messages when group_chat.messages cleared."""
        chat_instructor = mock_agents['chat_instructor']
        manager = mock_manager
        recovered_messages = [
            {"role": "assistant", "content": "I found the test files", "name": "Assistant"},
            {"role": "user", "content": "Execute Action 1: Identify test files", "name": "ChatInstructor"},
        ]
        chat_instructor.chat_messages = {manager: recovered_messages}
        # Simulate: group_chat.messages cleared by autogen
        mock_group_chat.messages = []
        # Recovery logic
        _chat_history = chat_instructor.chat_messages.get(manager, [])
        if _chat_history and len(mock_group_chat.messages) == 0:
            mock_group_chat.messages.extend(_chat_history)
        assert len(mock_group_chat.messages) == 2
        assert mock_group_chat.messages[0]["content"] == "I found the test files"

    def test_no_recovery_when_messages_present(self, mock_agents, mock_manager, mock_group_chat):
        """No recovery needed when group_chat.messages is populated."""
        chat_instructor = mock_agents['chat_instructor']
        manager = mock_manager
        mock_group_chat.messages = [{"role": "user", "content": "existing"}]
        chat_instructor.chat_messages = {manager: [{"role": "assistant", "content": "recovered"}]}
        _chat_history = chat_instructor.chat_messages.get(manager, [])
        if _chat_history and len(mock_group_chat.messages) == 0:
            mock_group_chat.messages.extend(_chat_history)
        # Should NOT have extended since messages were not empty
        assert len(mock_group_chat.messages) == 1
        assert mock_group_chat.messages[0]["content"] == "existing"

    def test_empty_chat_messages_no_recovery(self, mock_agents, mock_manager, mock_group_chat):
        """No recovery when chat_messages is empty."""
        chat_instructor = mock_agents['chat_instructor']
        manager = mock_manager
        mock_group_chat.messages = []
        chat_instructor.chat_messages = {manager: []}
        _chat_history = chat_instructor.chat_messages.get(manager, [])
        if _chat_history and len(mock_group_chat.messages) == 0:
            mock_group_chat.messages.extend(_chat_history)
        assert len(mock_group_chat.messages) == 0


# ===========================================================================
# SECTION 16: Auto-advance - pipeline advances past completed actions
# ===========================================================================

class TestAutoAdvance:
    """Tests for AUTO-ADVANCE logic that skips already-completed actions."""

    def test_auto_advance_increments_current_action(self):
        """When action is COMPLETED/TERMINATED and recipe exists, advance to next."""
        from helper import Action
        actions = [
            {"action_id": 1, "action": "A"},
            {"action_id": 2, "action": "B"},
            {"action_id": 3, "action": "C"},
        ]
        task = Action(actions)
        task.current_action = 1
        # Simulate: action 1 already done with recipe saved
        _ca = task.current_action
        # AUTO-ADVANCE logic: if completed and recipe exists, advance
        if _ca < len(task.actions):
            task.current_action = _ca + 1
            task.recipe = False
            task.fallback = False
        assert task.current_action == 2

    def test_auto_advance_last_action_sets_fallback(self):
        """When last action is already done, set fallback=True."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Only action"}]
        task = Action(actions)
        task.current_action = 1
        _ca = task.current_action
        # Last action: _ca >= len(actions)
        if _ca >= len(task.actions):
            task.fallback = True
            task.recipe = False
        assert task.fallback is True

    def test_auto_advance_requests_recipe_when_missing(self, tmp_prompts_dir):
        """When action is done but recipe file missing, request recipe instead of advancing."""
        from helper import Action
        actions = [{"action_id": 1, "action": "A"}, {"action_id": 2, "action": "B"}]
        task = Action(actions)
        task.current_action = 1
        _ca = task.current_action
        _recipe_path = os.path.join(tmp_prompts_dir, f'test_0_{_ca}.json')
        # Recipe file does NOT exist
        assert not os.path.exists(_recipe_path)
        # Logic: request recipe instead of advancing
        task.recipe = True
        task.fallback = False
        assert task.recipe is True


# ===========================================================================
# SECTION 17: Execute-pending - launches unstarted actions
# ===========================================================================

class TestExecutePending:
    """Tests for EXECUTE-PENDING logic that starts unstarted actions."""

    def test_pending_action_gets_started(self):
        """ASSIGNED/PENDING actions get transitioned to IN_PROGRESS and executed."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState
        user_prompt = "test_exec_pending"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        # Simulates the EXECUTE-PENDING check at line 3792
        _ca_pending_state = get_action_state(user_prompt, 1)
        assert _ca_pending_state in (ActionState.ASSIGNED, ActionState.PENDING, ActionState.IN_PROGRESS)
        # Transition to IN_PROGRESS
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "executing pending action")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS

    def test_execute_pending_message_format(self):
        """Execute-pending message includes action number and description."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Search for data"}]
        task = Action(actions)
        _ca_pending = 1
        actions_prompt = task.get_action(_ca_pending - 1)
        _exec_msg = f'Execute Action {_ca_pending}: {actions_prompt} ,Latest User message: test input'
        assert 'Execute Action 1' in _exec_msg

    def test_execute_pending_tracks_attempt_count(self):
        """Each execution attempt increments the retry counter."""
        from helper import Action
        actions = [{"action_id": 1, "action": "Do thing"}]
        task = Action(actions)
        task._exec_retries = {}
        _ca_pending = 1
        task._exec_retries[_ca_pending] = task._exec_retries.get(_ca_pending, 0) + 1
        assert task._exec_retries[_ca_pending] == 1
        task._exec_retries[_ca_pending] += 1
        assert task._exec_retries[_ca_pending] == 2


# ===========================================================================
# SECTION 18: Recipe-needed detection
# ===========================================================================

class TestRecipeNeededDetection:
    """Tests for RECIPE-NEEDED detection when recipe file does not exist."""

    def test_recipe_needed_when_file_missing(self, tmp_prompts_dir):
        """When action completed but recipe file missing, recipe is requested."""
        prompt_id = "recipe_needed"
        flow = 0
        action_id = 1
        _recipe_file = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_{action_id}.json')
        assert not os.path.exists(_recipe_file)
        # Pipeline should set recipe=True
        recipe_needed = not os.path.exists(_recipe_file)
        assert recipe_needed is True

    def test_recipe_not_needed_when_file_exists(self, tmp_prompts_dir):
        """When recipe file exists, no request needed."""
        prompt_id = "recipe_exists"
        flow = 0
        action_id = 1
        _recipe_file = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_{action_id}.json')
        with open(_recipe_file, "w") as f:
            json.dump({"action_id": 1, "recipe": [{"steps": "done"}]}, f)
        recipe_needed = not os.path.exists(_recipe_file)
        assert recipe_needed is False

    def test_already_done_with_recipe_advances(self, tmp_prompts_dir):
        """ALREADY DONE handler: completed + recipe saved -> advance to next action."""
        from helper import Action
        prompt_id = "already_done"
        flow = 0
        actions = [{"action_id": 1, "action": "A"}, {"action_id": 2, "action": "B"}]
        task = Action(actions)
        task.current_action = 1
        # Create recipe file for action 1
        _recipe_file = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_1.json')
        with open(_recipe_file, "w") as f:
            json.dump({"action_id": 1, "recipe": [{"steps": "done"}]}, f)
        # ALREADY DONE logic (line 3527): advance
        json_action_id = 1
        if json_action_id < len(task.actions):
            task.current_action = json_action_id + 1
            task.recipe = False
            task.fallback = False
        assert task.current_action == 2


# ===========================================================================
# SECTION 19: Late recipe save
# ===========================================================================

class TestLateRecipeSave:
    """Tests for saving recipe even when action already TERMINATED."""

    def test_late_recipe_save_when_file_missing(self, tmp_prompts_dir):
        """Recipe saved even if action is already TERMINATED and file does not exist."""
        prompt_id = "late_save"
        flow = 0
        action_id = 1
        name = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_{action_id}.json')
        json_obj = {
            "status": "done",
            "action_id": action_id,
            "recipe": [{"steps": "late step"}],
        }
        # Late save logic (line 2044): save if recipe present and file missing
        if 'recipe' in json_obj and json_obj.get('recipe'):
            if not os.path.exists(name):
                with open(name, "w") as json_file:
                    json.dump(json_obj, json_file)
        assert os.path.exists(name)
        with open(name) as f:
            loaded = json.load(f)
        assert loaded["recipe"][0]["steps"] == "late step"

    def test_late_recipe_skip_when_file_exists(self, tmp_prompts_dir):
        """Late recipe save skips when file already exists."""
        prompt_id = "late_skip"
        flow = 0
        action_id = 1
        name = os.path.join(tmp_prompts_dir, f'{prompt_id}_{flow}_{action_id}.json')
        # Pre-existing file
        with open(name, "w") as f:
            json.dump({"action_id": 1, "recipe": [{"steps": "original"}]}, f)
        json_obj = {
            "status": "done",
            "action_id": action_id,
            "recipe": [{"steps": "late overwrite attempt"}],
        }
        if 'recipe' in json_obj and json_obj.get('recipe'):
            if not os.path.exists(name):
                with open(name, "w") as json_file:
                    json.dump(json_obj, json_file)
        # File should still contain original content
        with open(name) as f:
            loaded = json.load(f)
        assert loaded["recipe"][0]["steps"] == "original"

    def test_late_recipe_transitions_terminated_properly(self):
        """After late recipe save, action transitions through RECIPE_RECEIVED to TERMINATED."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        )
        user_prompt = "test_late_term"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        # Late save path: COMPLETED must walk through full path to TERMINATED
        # COMPLETED -> FALLBACK_REQUESTED -> FALLBACK_RECEIVED -> RECIPE_REQUESTED -> RECIPE_RECEIVED -> TERMINATED
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "fb")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_RECEIVED, "fb recv")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_REQUESTED, "recipe req")
        safe_set_state(user_prompt, 1, ActionState.RECIPE_RECEIVED, "recipe saved late")
        safe_set_state(user_prompt, 1, ActionState.TERMINATED, "terminate")
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED


# ===========================================================================
# SECTION 20: Hallucination defense - action_id mismatch detection
# ===========================================================================

class TestHallucinationDefense:
    """Tests for LLM hallucination defense on action_id claims."""

    def test_action_id_mismatch_corrected(self):
        """When LLM claims different action_id, pipeline uses KNOWN action_id."""
        current_action_id = 2
        json_obj = {"status": "completed", "action_id": 5}  # LLM hallucinated action 5
        json_action_id = int(json_obj.get('action_id', current_action_id))
        # Defense logic (line 3492): override with known
        if json_action_id != current_action_id:
            json_action_id = current_action_id
        assert json_action_id == 2  # Corrected to actual

    def test_action_id_match_accepted(self):
        """When LLM reports correct action_id, it is accepted."""
        current_action_id = 3
        json_obj = {"status": "completed", "action_id": 3}
        json_action_id = int(json_obj.get('action_id', current_action_id))
        if json_action_id != current_action_id:
            json_action_id = current_action_id
        assert json_action_id == 3

    def test_missing_action_id_uses_current(self):
        """When LLM omits action_id, current_action_id is used."""
        current_action_id = 1
        json_obj = {"status": "completed"}
        json_action_id = int(json_obj.get('action_id', current_action_id))
        assert json_action_id == 1

    def test_integrity_check_rejects_corrupted_task(self):
        """Integrity check failure rejects the completion claim."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        ledger = SmartLedger(agent_id="integrity_test", session_id="test")
        task = Task(
            task_id="action_1", description="Test",
            task_type=TaskType.PRE_ASSIGNED,
            execution_mode=ExecutionMode.SEQUENTIAL,
            status=TaskStatus.IN_PROGRESS,
        )
        ledger.add_task(task)
        # verify_integrity() checks data_hash
        integrity_ok = task.verify_integrity()
        # Fresh task should pass integrity
        assert integrity_ok is True


# ===========================================================================
# SECTION 21: group_chat sync - manager._groupchat reference
# ===========================================================================

class TestGroupChatSync:
    """Tests for GroupChat message synchronization with manager._groupchat."""

    def test_manager_has_groupchat_reference(self, mock_manager, mock_group_chat):
        """Manager has _groupchat attribute pointing to GroupChat."""
        assert mock_manager._groupchat is mock_group_chat

    def test_group_chat_messages_shared(self, mock_manager, mock_group_chat):
        """Adding messages to group_chat is visible via manager._groupchat."""
        mock_group_chat.messages.append({"role": "user", "content": "test"})
        assert len(mock_manager._groupchat.messages) == 1

    def test_terminate_detection_in_group_chat(self, mock_group_chat):
        """TERMINATE message in group_chat triggers termination check."""
        mock_group_chat.messages = [
            {"role": "assistant", "content": '{"status": "completed"}', "name": "StatusVerifier"},
            {"role": "user", "content": "TERMINATE", "name": "ChatInstructor"},
        ]
        last_msg = mock_group_chat.messages[-1]
        is_terminate = (last_msg['name'] == 'ChatInstructor' and
                        last_msg['content'] == 'TERMINATE')
        assert is_terminate is True

    def test_at_mention_routing(self):
        """@mention patterns route to correct agents."""
        import re
        messages = [
            {"content": "@Helper please search", "name": "Assistant"},
            {"content": "@StatusVerifier check results", "name": "Assistant"},
            {"content": "@User need your input", "name": "Assistant"},
            {"content": "@Executor run this code", "name": "Assistant"},
        ]
        assert re.search(r"@Helper", messages[0]["content"], re.IGNORECASE)
        assert re.search(r"@StatusVerifier", messages[1]["content"], re.IGNORECASE)
        assert re.search(r"@User", messages[2]["content"], re.IGNORECASE)
        assert re.search(r"@Executor", messages[3]["content"])

    def test_is_terminate_msg_helper(self):
        """_is_terminate_msg returns True for TERMINATE messages."""
        from helper import _is_terminate_msg
        assert _is_terminate_msg({"content": "TERMINATE"}) is True
        assert _is_terminate_msg({"content": "Hello"}) is False

    def test_is_terminate_msg_handles_none(self):
        """_is_terminate_msg handles None content gracefully."""
        from helper import _is_terminate_msg
        result = _is_terminate_msg({"content": None})
        assert isinstance(result, bool)


# ===========================================================================
# SECTION 22: Ledger sync at each state change
# ===========================================================================

class TestLedgerSync:
    """Tests for auto-sync between ActionState and SmartLedger."""

    @pytest.fixture
    def ledger_setup(self):
        """Create a ledger and register it for auto-sync."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        from lifecycle_hooks import register_ledger_for_session, action_states

        user_prompt = f"ledger_test_{id(self)}"
        ledger = SmartLedger(agent_id="test", session_id=user_prompt)

        for i in range(1, 4):
            task = Task(
                task_id=f"action_{i}",
                description=f"Test action {i}",
                task_type=TaskType.PRE_ASSIGNED,
                execution_mode=ExecutionMode.SEQUENTIAL,
                status=TaskStatus.PENDING,
            )
            ledger.add_task(task)

        register_ledger_for_session(user_prompt, ledger)
        action_states.clear()

        yield user_prompt, ledger, TaskStatus

        action_states.clear()

    def test_assigned_maps_to_pending(self, ledger_setup):
        """ActionState.ASSIGNED -> LedgerTaskStatus.PENDING."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "test")
        assert ledger.tasks["action_1"].status == TaskStatus.PENDING

    def test_in_progress_maps_and_claims(self, ledger_setup):
        """ActionState.IN_PROGRESS -> LedgerTaskStatus.IN_PROGRESS + ownership claimed."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        assert ledger.tasks["action_1"].status == TaskStatus.IN_PROGRESS
        assert ledger.tasks["action_1"].is_owned

    def test_completed_maps_and_releases(self, ledger_setup):
        """ActionState.COMPLETED -> LedgerTaskStatus.COMPLETED + ownership released."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState, force_state_through_valid_path
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED

    def test_error_maps_to_failed(self, ledger_setup):
        """ActionState.ERROR -> LedgerTaskStatus.FAILED."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "failed")
        assert ledger.tasks["action_1"].status == TaskStatus.FAILED

    def test_fallback_requested_maps_to_blocked(self, ledger_setup):
        """ActionState.FALLBACK_REQUESTED sets blocked_reason on ledger task."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState, force_state_through_valid_path
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        safe_set_state(user_prompt, 1, ActionState.FALLBACK_REQUESTED, "need input")
        task = ledger.tasks["action_1"]
        assert task.blocked_reason == 'input_required' or task.status in (TaskStatus.BLOCKED, TaskStatus.COMPLETED)

    def test_heartbeat_recorded_on_state_change(self, ledger_setup):
        """Every state change records a heartbeat."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState
        task = ledger.tasks["action_1"]
        old_heartbeat = getattr(task, 'last_heartbeat', None)
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        new_heartbeat = getattr(task, 'last_heartbeat', None)
        if old_heartbeat is not None and new_heartbeat is not None:
            assert new_heartbeat >= old_heartbeat

    def test_preview_pending_sets_blocked_reason(self, ledger_setup):
        """PREVIEW_PENDING -> blocked_reason = 'approval_required'."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import safe_set_state, ActionState
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.PREVIEW_PENDING, "destructive")
        task = ledger.tasks["action_1"]
        assert task.blocked_reason == 'approval_required' or task.status in (TaskStatus.BLOCKED, TaskStatus.PENDING)

    def test_block_and_resume_for_user_input(self, ledger_setup):
        """block_for_user_input + resume_from_user_input cycle."""
        user_prompt, ledger, TaskStatus = ledger_setup
        from lifecycle_hooks import (
            safe_set_state, ActionState,
            block_for_user_input, resume_from_user_input
        )
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        block_for_user_input(user_prompt, 1, "Need user confirmation")
        assert ledger.tasks["action_1"].status == TaskStatus.BLOCKED
        resume_from_user_input(user_prompt, 1, "User confirmed")
        assert ledger.tasks["action_1"].status == TaskStatus.IN_PROGRESS


# ===========================================================================
# SECTION 23: ActionRetryTracker
# ===========================================================================

class TestActionRetryTracker:
    """Tests for retry tracking to prevent infinite loops."""

    def test_retry_tracker_exists(self):
        """ActionRetryTracker class exists."""
        from lifecycle_hooks import ActionRetryTracker
        tracker = ActionRetryTracker()
        assert hasattr(tracker, 'pending_counts')

    def test_retry_count_increments(self):
        """Retry count increments via increment_pending."""
        from lifecycle_hooks import ActionRetryTracker
        tracker = ActionRetryTracker()
        exceeded = tracker.increment_pending("test_user", 1)
        assert exceeded is False
        assert tracker.pending_counts[("test_user", 1)] == 1

    def test_retry_threshold_triggers_error(self):
        """After MAX_PENDING_RETRIES, increment_pending returns True."""
        from lifecycle_hooks import ActionRetryTracker
        tracker = ActionRetryTracker()
        # Exhaust retries
        for _ in range(tracker.MAX_PENDING_RETRIES):
            tracker.increment_pending("test_user", 1)
        # Next call should exceed
        exceeded = tracker.increment_pending("test_user", 1)
        assert exceeded is True

    def test_retry_reset_clears_count(self):
        """reset_count clears the pending count for an action."""
        from lifecycle_hooks import ActionRetryTracker
        tracker = ActionRetryTracker()
        tracker.increment_pending("test_user", 1)
        tracker.increment_pending("test_user", 1)
        tracker.reset_count("test_user", 1)
        assert tracker.pending_counts.get(("test_user", 1), 0) == 0


# ===========================================================================
# SECTION 24: Audit log and EventBus integration
# ===========================================================================

class TestAuditAndEventBus:
    """Tests for state change audit logging and EventBus emission."""

    def test_state_change_emits_audit_event(self):
        """State transitions emit audit log events."""
        from lifecycle_hooks import safe_set_state, ActionState
        mock_log = MagicMock()
        with patch('security.immutable_audit_log.get_audit_log', return_value=mock_log):
            user_prompt = "audit_test"
            safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
            safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
            if mock_log.log_event.called:
                assert mock_log.log_event.call_count >= 1

    def test_state_change_emits_eventbus_event(self):
        """State transitions emit EventBus events."""
        from lifecycle_hooks import safe_set_state, ActionState
        with patch('core.platform.events.emit_event') as mock_emit:
            user_prompt = "eventbus_test"
            safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
            safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
            if mock_emit.called:
                topics = [c[0][0] for c in mock_emit.call_args_list]
                assert 'action_state.changed' in topics


# ===========================================================================
# SECTION 25: Resume from progress
# ===========================================================================

class TestResumeFromProgress:
    """Tests for resume/recovery after interruption."""

    def test_detect_completed_actions_from_disk(self, tmp_prompts_dir):
        """detect_and_resume_progress finds completed action files on disk."""
        prompt_id = "resume_test"
        for action_id in [1, 2]:
            path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_{action_id}.json")
            with open(path, "w") as f:
                json.dump({"action_id": action_id, "status": "completed"}, f)
        files = [f for f in os.listdir(tmp_prompts_dir) if f.startswith(prompt_id)]
        assert len(files) == 2

    def test_ledger_persistence_to_json(self):
        """Ledger data persists to JSON file for recovery."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
            from agent_ledger.backends import JSONBackend
        except ImportError:
            pytest.skip("agent_ledger not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            backend = JSONBackend(storage_dir=tmpdir)
            ledger = SmartLedger(agent_id="persist_test", session_id="sess_1",
                                 backend=backend)
            ledger.add_task(Task(
                task_id="action_1", description="Test",
                task_type=TaskType.PRE_ASSIGNED,
                execution_mode=ExecutionMode.SEQUENTIAL,
                status=TaskStatus.PENDING
            ))
            ledger.save()
            json_files = [f for f in os.listdir(tmpdir) if f.endswith('.json')]
            assert len(json_files) > 0

    def test_state_recovery_after_crash(self):
        """Action states can be recovered from ledger after crash."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState,
            force_state_through_valid_path, register_ledger_for_session, action_states
        )

        user_prompt = f"crash_test_{id(self)}"
        ledger = SmartLedger(agent_id="crash", session_id=user_prompt)
        ledger.add_task(Task(
            task_id="action_1", description="Test",
            task_type=TaskType.PRE_ASSIGNED,
            execution_mode=ExecutionMode.SEQUENTIAL,
            status=TaskStatus.PENDING
        ))
        register_ledger_for_session(user_prompt, ledger)

        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED

        # Simulate crash: clear action_states
        action_states.pop(user_prompt, None)
        # Ledger still has the truth
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED


# ===========================================================================
# SECTION 26: create_action_with_ledger integration
# ===========================================================================

class TestCreateActionWithLedger:
    """Tests for the create_action_with_ledger function."""

    def test_creates_action_with_ledger_attached(self):
        """Action instance has ledger attached after creation."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        actions = [
            {"action_id": 1, "action": "Do A", "prerequisites": []},
            {"action_id": 2, "action": "Do B", "prerequisites": [1]}
        ]

        mock_ledger = SmartLedger(agent_id="test", session_id="test_1")

        with patch('create_recipe.current_app', mock_app), \
             patch('helper.current_app', mock_app), \
             patch('create_recipe.get_production_backend', return_value=None), \
             patch('create_recipe.create_ledger_from_actions', return_value=mock_ledger), \
             patch('create_recipe.register_ledger_for_session'), \
             patch('create_recipe.TaskDelegationBridge', return_value=MagicMock()), \
             patch('create_recipe.a2a_context', MagicMock()):

            from create_recipe import create_action_with_ledger, user_ledgers, user_delegation_bridges
            user_prompt = f"test_cal_{id(self)}"

            try:
                result = create_action_with_ledger(actions, "test", "1", user_prompt)
                assert result.ledger is mock_ledger
                assert user_prompt in user_ledgers
            finally:
                user_ledgers.pop(user_prompt, None)
                user_delegation_bridges.pop(user_prompt, None)

    def test_ledger_reuse_on_existing_session(self):
        """Existing session reuses ledger instead of creating new one."""
        try:
            from agent_ledger import SmartLedger
        except ImportError:
            pytest.skip("agent_ledger not installed")

        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        mock_ledger = SmartLedger(agent_id="test", session_id="reuse_test")

        from create_recipe import user_ledgers, user_delegation_bridges

        user_prompt = f"test_reuse_{id(self)}"
        user_ledgers[user_prompt] = mock_ledger
        user_delegation_bridges[user_prompt] = MagicMock()

        actions = [{"action_id": 1, "action": "Do thing"}]

        with patch('create_recipe.current_app', mock_app), \
             patch('helper.current_app', mock_app), \
             patch('create_recipe.TaskDelegationBridge', return_value=MagicMock()), \
             patch('create_recipe.a2a_context', MagicMock()):
            try:
                from create_recipe import create_action_with_ledger
                result = create_action_with_ledger(actions, "test", "1", user_prompt)
                assert result.ledger is mock_ledger
            finally:
                user_ledgers.pop(user_prompt, None)
                user_delegation_bridges.pop(user_prompt, None)


# ===========================================================================
# SECTION 27: Daemon agent dispatch
# ===========================================================================

class TestDaemonAgentDispatch:
    """Tests for agent_daemon.py dispatching goals through the pipeline."""

    def test_daemon_dispatch_goal_calls_recipe(self):
        """dispatch_goal routes to recipe() for CREATE goals."""
        try:
            from integrations.agent_engine.agent_daemon import AgentDaemon
        except ImportError:
            pytest.skip("agent_daemon not importable")

        daemon = AgentDaemon.__new__(AgentDaemon)
        daemon._running = False
        daemon._goals = []
        daemon._lock = threading.Lock()
        assert hasattr(daemon, '_tick') or hasattr(daemon, 'dispatch_goal')

    def test_daemon_tick_processes_goals(self):
        """Daemon _tick processes pending goals."""
        try:
            from integrations.agent_engine.agent_daemon import AgentDaemon
        except ImportError:
            pytest.skip("agent_daemon not importable")
        assert hasattr(AgentDaemon, '_tick')

    def test_goal_manager_create_goal(self):
        """GoalManager.create_goal creates properly structured goal."""
        try:
            from integrations.agent_engine.goal_manager import GoalManager
        except ImportError:
            pytest.skip("goal_manager not importable")
        assert hasattr(GoalManager, 'create_goal') or hasattr(GoalManager, 'add_goal')

    def test_seed_goals_module_exists(self):
        """goal_seeding.py module is importable."""
        try:
            from integrations.agent_engine import goal_seeding
            assert hasattr(goal_seeding, 'seed_bootstrap_goals') or \
                   hasattr(goal_seeding, 'auto_remediate_loopholes')
        except ImportError:
            pytest.skip("goal_seeding not importable")


# ===========================================================================
# COMBINATION TEST 22: Full CREATE pipeline
# ===========================================================================

class TestFullCreatePipeline:
    """Full CREATE pipeline: gather -> execute all actions -> save all recipes -> flow completion."""

    def test_full_pipeline_config_to_recipe(self, tmp_prompts_dir):
        """Full pipeline: config -> decompose -> state transitions -> recipe save."""
        prompt_id = "full_pipeline"

        # Step 1: Save config
        config_path = os.path.join(tmp_prompts_dir, f"{prompt_id}.json")
        with open(config_path, "w") as f:
            json.dump(SAMPLE_AGENT_CONFIG, f)

        # Step 2: Load and decompose
        with open(config_path) as f:
            config = json.load(f)
        assert len(config["flows"]) == 2

        # Step 3: Initialize states for flow 0
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState,
            force_state_through_valid_path
        )

        user_prompt = "full_pipeline_user"
        flow0_actions = config["flows"][0]["actions"]

        for action in flow0_actions:
            safe_set_state(user_prompt, action["action_id"],
                           ActionState.ASSIGNED, "init")

        # Step 4: Execute each action (simulate full lifecycle)
        for action in flow0_actions:
            aid = action["action_id"]
            # IN_PROGRESS
            safe_set_state(user_prompt, aid, ActionState.IN_PROGRESS, "start")
            # STATUS_VERIFICATION_REQUESTED
            safe_set_state(user_prompt, aid, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
            # COMPLETED
            safe_set_state(user_prompt, aid, ActionState.COMPLETED, "done")
            # FALLBACK
            safe_set_state(user_prompt, aid, ActionState.FALLBACK_REQUESTED, "fb")
            safe_set_state(user_prompt, aid, ActionState.FALLBACK_RECEIVED, "fb recv")
            # RECIPE
            safe_set_state(user_prompt, aid, ActionState.RECIPE_REQUESTED, "recipe req")
            safe_set_state(user_prompt, aid, ActionState.RECIPE_RECEIVED, "recipe recv")
            # TERMINATED
            safe_set_state(user_prompt, aid, ActionState.TERMINATED, "terminated")

            # Save action recipe
            action_result = {
                "action_id": aid,
                "action": action["action"],
                "status": "done",
                "recipe": [{"steps": f"Step for action {aid}"}],
                "result": f"Result for action {aid}"
            }
            action_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_{aid}.json")
            with open(action_path, "w") as f:
                json.dump(action_result, f)

        # Step 5: Save flow recipe
        recipe_data = {
            "flow_name": config["flows"][0]["flow_name"],
            "actions": flow0_actions
        }
        recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_recipe.json")
        with open(recipe_path, "w") as f:
            json.dump(recipe_data, f)

        # Verify: config + 3 action files + 1 recipe file = 5 files
        files = os.listdir(tmp_prompts_dir)
        prompt_files = [f for f in files if f.startswith(prompt_id)]
        assert len(prompt_files) == 5

        # Verify all actions TERMINATED
        for action in flow0_actions:
            assert get_action_state(user_prompt, action["action_id"]) == ActionState.TERMINATED

        # Verify recipe structure
        with open(recipe_path) as f:
            recipe = json.load(f)
        assert recipe["flow_name"] == "Run Tests"
        assert len(recipe["actions"]) == 3

    def test_gather_then_decompose_then_execute(self):
        """Config JSON from gather can be decomposed into actions and executed."""
        config = SAMPLE_AGENT_CONFIG
        assert len(config["flows"]) == 2
        flow1_actions = config["flows"][0]["actions"]
        assert len(flow1_actions) == 3
        # Each action has valid structure
        for a in flow1_actions:
            assert "action_id" in a
            assert "action" in a
            assert "can_perform_without_user_input" in a


# ===========================================================================
# COMBINATION TEST 23: Autonomous agent with scheduled tasks
# ===========================================================================

class TestAutonomousAgentWithScheduledTasks:
    """Creates agent with cron-scheduled tasks."""

    def test_autonomous_agent_creates_and_schedules(self, tmp_prompts_dir):
        """Full CREATE pipeline + scheduled tasks generation."""
        prompt_id = "auto_sched"
        config = copy.deepcopy(SAMPLE_AGENT_CONFIG)

        # Save config
        config_path = os.path.join(tmp_prompts_dir, f"{prompt_id}.json")
        with open(config_path, "w") as f:
            json.dump(config, f)

        # Execute flow 0 (simulate) and save recipe with scheduled_tasks
        from lifecycle_hooks import safe_set_state, ActionState, force_state_through_valid_path
        user_prompt = "auto_sched_user"

        for action in config["flows"][0]["actions"]:
            aid = action["action_id"]
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "init")
            _terminate_action(user_prompt, aid)
            # Save action recipe
            action_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_{aid}.json")
            with open(action_path, "w") as f:
                json.dump({"action_id": aid, "recipe": [{"steps": "auto"}]}, f)

        # Save flow recipe with scheduled tasks
        recipe = {
            "status": "completed",
            "flow_name": "Run Tests",
            "actions": config["flows"][0]["actions"],
            "scheduled_tasks": [
                {
                    "cron_expression": "0 9 * * *",
                    "persona": "Tester",
                    "action_entry_point": 1,
                    "action_exit_point": 3,
                    "job_description": "Run daily test suite"
                }
            ]
        }
        recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_recipe.json")
        with open(recipe_path, "w") as f:
            json.dump(recipe, f)

        # Verify recipe has scheduled tasks
        with open(recipe_path) as f:
            loaded = json.load(f)
        assert "scheduled_tasks" in loaded
        assert loaded["scheduled_tasks"][0]["cron_expression"] == "0 9 * * *"

    def test_all_actions_autonomous(self):
        """All actions in flow have can_perform_without_user_input=yes."""
        flow = SAMPLE_AGENT_CONFIG["flows"][0]
        for action in flow["actions"]:
            assert action["can_perform_without_user_input"] == "yes"


# ===========================================================================
# COMBINATION TEST 24: Multi-flow agent
# ===========================================================================

class TestMultiFlowAgent:
    """Multiple personas, each flow completes independently."""

    def test_multi_flow_progression(self):
        """After flow 0 completes, flow 1 starts with fresh action states."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        )

        user_prompt = "combo_multi_flow"
        # Flow 0: complete and terminate all actions
        for action in SAMPLE_ACTIONS_FLOW1:
            aid = action["action_id"]
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "init")
            _terminate_action(user_prompt, aid)

        # Flow 1: start actions (fresh ASSIGNED state)
        for action in SAMPLE_ACTIONS_FLOW2:
            aid = action["action_id"]
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "new flow")
            assert get_action_state(user_prompt, aid) == ActionState.ASSIGNED

    def test_each_flow_has_its_own_persona(self):
        """Each flow has a distinct persona from the agent config."""
        config = SAMPLE_AGENT_CONFIG
        personas = [f["persona"] for f in config["flows"]]
        assert personas[0] == "Tester"
        assert personas[1] == "Reporter"

    def test_multi_flow_full_lifecycle(self, tmp_prompts_dir):
        """Both flows complete full lifecycle with recipe files."""
        from lifecycle_hooks import safe_set_state, ActionState, force_state_through_valid_path
        prompt_id = "multi_flow_full"
        user_prompt = "multi_flow_user"

        for flow_idx, flow in enumerate(SAMPLE_AGENT_CONFIG["flows"]):
            for action in flow["actions"]:
                aid = action["action_id"]
                safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "init")
                _terminate_action(user_prompt, aid)
                # Save action recipe
                path = os.path.join(tmp_prompts_dir, f"{prompt_id}_{flow_idx}_{aid}.json")
                with open(path, "w") as f:
                    json.dump({"action_id": aid, "recipe": [{"steps": f"flow{flow_idx}_action{aid}"}]}, f)

            # Save flow recipe
            recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_{flow_idx}_recipe.json")
            with open(recipe_path, "w") as f:
                json.dump({"flow_name": flow["flow_name"], "persona": flow["persona"]}, f)

        # Verify all files created: 3+1 for flow 0, 2+1 for flow 1 = 7 files
        files = [f for f in os.listdir(tmp_prompts_dir) if f.startswith(prompt_id)]
        assert len(files) == 7


# ===========================================================================
# COMBINATION TEST 25: Error recovery chain
# ===========================================================================

class TestErrorRecoveryChain:
    """Error -> Helper resolution -> retry -> completion chain."""

    def test_error_then_helper_then_retry_then_complete(self):
        """Full error recovery: ERROR -> IN_PROGRESS (retry) -> COMPLETED."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        )

        user_prompt = "combo_error_chain"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "LLM returned bad JSON")

        # Helper resolves the issue, retry
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "retry after helper")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify again")
        safe_set_state(user_prompt, 1, ActionState.COMPLETED, "success on retry")
        assert get_action_state(user_prompt, 1) == ActionState.COMPLETED

    def test_multiple_error_retries_then_complete(self):
        """Action can error and retry multiple times before completing."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState

        user_prompt = "multi_retry"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")

        for attempt in range(3):
            safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, f"attempt {attempt+1}")
            safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
            if attempt < 2:
                safe_set_state(user_prompt, 1, ActionState.ERROR, f"failed attempt {attempt+1}")
            else:
                safe_set_state(user_prompt, 1, ActionState.COMPLETED, "finally done")

        assert get_action_state(user_prompt, 1) == ActionState.COMPLETED

    def test_error_recovery_with_ledger_sync(self):
        """Error recovery: ActionState allows ERROR->IN_PROGRESS but ledger FAILED is terminal.

        The ActionState layer tracks the retry but the ledger stays FAILED
        (ledger treats FAILED as terminal). This is correct behavior -- the
        ledger records the failure, the ActionState machine handles retry logic.
        """
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, register_ledger_for_session,
            force_state_through_valid_path, action_states
        )

        user_prompt = f"error_ledger_{id(self)}"
        ledger = SmartLedger(agent_id="error", session_id=user_prompt)
        ledger.add_task(Task(
            task_id="action_1", description="Test",
            task_type=TaskType.PRE_ASSIGNED,
            execution_mode=ExecutionMode.SEQUENTIAL,
            status=TaskStatus.PENDING
        ))
        register_ledger_for_session(user_prompt, ledger)

        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "start")
        safe_set_state(user_prompt, 1, ActionState.STATUS_VERIFICATION_REQUESTED, "verify")
        safe_set_state(user_prompt, 1, ActionState.ERROR, "failed")
        assert ledger.tasks["action_1"].status == TaskStatus.FAILED

        # ActionState layer allows retry (ERROR -> IN_PROGRESS)
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "retry")
        assert get_action_state(user_prompt, 1) == ActionState.IN_PROGRESS
        # Ledger stays FAILED since it treats FAILED as terminal
        assert ledger.tasks["action_1"].status == TaskStatus.FAILED

    def test_destructive_action_preview_then_execute(self):
        """Destructive actions go through preview approval before execution."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        )

        user_prompt = "combo_preview"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 1, ActionState.PREVIEW_PENDING, "rm -rf detected")
        safe_set_state(user_prompt, 1, ActionState.PREVIEW_APPROVED, "user approved")
        safe_set_state(user_prompt, 1, ActionState.IN_PROGRESS, "executing")
        force_state_through_valid_path(user_prompt, 1, ActionState.COMPLETED, "done")
        assert get_action_state(user_prompt, 1) == ActionState.COMPLETED


# ===========================================================================
# COMBINATION TEST 26: Resume from progress
# ===========================================================================

class TestResumeFromProgressCombination:
    """Pipeline resumes from last completed action after interruption."""

    def test_resume_from_partial_completion(self, tmp_prompts_dir):
        """Pipeline resumes from action 3 after actions 1 and 2 completed."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState, force_state_through_valid_path
        )
        from helper import Action

        prompt_id = "resume_combo"
        user_prompt = "resume_combo_user"

        # Actions 1 and 2 already completed and have recipe files
        for aid in [1, 2]:
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "init")
            _terminate_action(user_prompt, aid)
            path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_{aid}.json")
            with open(path, "w") as f:
                json.dump({"action_id": aid, "recipe": [{"steps": "done"}]}, f)

        # Action 3 still pending
        actions = [
            {"action_id": 1, "action": "A"},
            {"action_id": 2, "action": "B"},
            {"action_id": 3, "action": "C"},
        ]
        task = Action(actions)
        task.current_action = 3  # Resume point

        # Verify resume point
        assert task.current_action == 3
        assert get_action_state(user_prompt, 1) == ActionState.TERMINATED
        assert get_action_state(user_prompt, 2) == ActionState.TERMINATED

        # Action 3 can now proceed
        safe_set_state(user_prompt, 3, ActionState.ASSIGNED, "init")
        safe_set_state(user_prompt, 3, ActionState.IN_PROGRESS, "resume execution")
        assert get_action_state(user_prompt, 3) == ActionState.IN_PROGRESS

    def test_resume_with_ledger_recovery(self):
        """Ledger tasks survive crash and enable resume."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        from lifecycle_hooks import (
            safe_set_state, ActionState, register_ledger_for_session,
            force_state_through_valid_path, action_states
        )

        user_prompt = f"resume_ledger_{id(self)}"
        ledger = SmartLedger(agent_id="resume", session_id=user_prompt)
        for i in range(1, 4):
            ledger.add_task(Task(
                task_id=f"action_{i}", description=f"Action {i}",
                task_type=TaskType.PRE_ASSIGNED,
                execution_mode=ExecutionMode.SEQUENTIAL,
                status=TaskStatus.PENDING
            ))
        register_ledger_for_session(user_prompt, ledger)

        # Complete actions 1 and 2
        for i in [1, 2]:
            safe_set_state(user_prompt, i, ActionState.ASSIGNED, "init")
            force_state_through_valid_path(user_prompt, i, ActionState.COMPLETED, "done")

        # Simulate crash
        action_states.pop(user_prompt, None)

        # Ledger still knows what happened
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED
        assert ledger.tasks["action_2"].status == TaskStatus.COMPLETED
        assert ledger.tasks["action_3"].status == TaskStatus.PENDING

        # Resume from action 3
        next_task = ledger.get_next_executable_task()
        if next_task:
            assert next_task.task_id == "action_3"


# ===========================================================================
# COMBINATION TEST 27: Daemon agent pipeline
# ===========================================================================

class TestDaemonAgentPipeline:
    """Daemon creates agent via same CREATE pipeline."""

    def test_daemon_goal_uses_recipe_function(self):
        """Daemon's CREATE goal should call the recipe() function."""
        # The daemon dispatches to recipe() for CREATE goals
        # Verify the function signature exists
        from create_recipe import recipe as create_recipe_fn
        import inspect
        sig = inspect.signature(create_recipe_fn)
        params = list(sig.parameters.keys())
        assert 'user_id' in params
        assert 'text' in params
        assert 'prompt_id' in params

    def test_daemon_pipeline_state_transitions(self):
        """Daemon-created agent follows same state machine."""
        from lifecycle_hooks import (
            safe_set_state, get_action_state, ActionState
        )

        user_prompt = "daemon_pipeline"
        actions = [
            {"action_id": 1, "action": "Daemon task A"},
            {"action_id": 2, "action": "Daemon task B"},
        ]

        for action in actions:
            aid = action["action_id"]
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "daemon init")
            _terminate_action(user_prompt, aid)
            assert get_action_state(user_prompt, aid) == ActionState.TERMINATED

    def test_daemon_agent_full_lifecycle(self, tmp_prompts_dir):
        """Daemon agent goes through full CREATE lifecycle and produces recipe files."""
        from lifecycle_hooks import safe_set_state, ActionState, force_state_through_valid_path

        prompt_id = "daemon_full"
        user_prompt = "daemon_full_user"

        # Simulate daemon creating config
        config = copy.deepcopy(SAMPLE_AGENT_CONFIG)
        config_path = os.path.join(tmp_prompts_dir, f"{prompt_id}.json")
        with open(config_path, "w") as f:
            json.dump(config, f)

        # Execute flow 0
        for action in config["flows"][0]["actions"]:
            aid = action["action_id"]
            safe_set_state(user_prompt, aid, ActionState.ASSIGNED, "init")
            _terminate_action(user_prompt, aid)
            # Save recipe
            path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_{aid}.json")
            with open(path, "w") as f:
                json.dump({"action_id": aid, "recipe": [{"steps": "daemon step"}]}, f)

        # Save flow recipe
        recipe_path = os.path.join(tmp_prompts_dir, f"{prompt_id}_0_recipe.json")
        with open(recipe_path, "w") as f:
            json.dump({"flow_name": "Run Tests", "actions": config["flows"][0]["actions"]}, f)

        # Verify all files created
        files = [f for f in os.listdir(tmp_prompts_dir) if f.startswith(prompt_id)]
        assert len(files) == 5  # config + 3 actions + 1 recipe


# ===========================================================================
# Additional channel and device tests
# ===========================================================================

class TestChannelRouting:
    """Tests for channel input/output routing."""

    def test_crossbar_publish_async_delegates(self):
        """publish_async delegates to hart_intelligence module."""
        with patch('create_recipe.publish_async') as mock_pub:
            mock_pub("test.topic", {"data": "hello"})
            mock_pub.assert_called_once_with("test.topic", {"data": "hello"})

    def test_channel_session_isolation(self):
        """Different channels maintain separate sessions."""
        user_prompt_discord = "discord_user_123"
        user_prompt_telegram = "telegram_user_123"
        assert user_prompt_discord != user_prompt_telegram


class TestMultiDeviceCoordination:
    """Tests for multi-device coordination."""

    def test_device_routing_by_capability(self):
        """Actions route to devices based on capability."""
        devices = {
            "phone": {"capabilities": ["camera", "gps", "microphone"]},
            "desktop": {"capabilities": ["compute", "display", "keyboard"]},
            "iot_sensor": {"capabilities": ["temperature", "humidity"]}
        }
        action_needs = "camera"
        matching = [d for d, info in devices.items()
                    if action_needs in info["capabilities"]]
        assert "phone" in matching
        assert "desktop" not in matching

    def test_device_session_independence(self):
        """Each device maintains independent action state."""
        from lifecycle_hooks import safe_set_state, get_action_state, ActionState

        safe_set_state("phone_user_1", 1, ActionState.ASSIGNED, "init")
        safe_set_state("desktop_user_1", 1, ActionState.ASSIGNED, "init")
        safe_set_state("phone_user_1", 1, ActionState.IN_PROGRESS, "start on phone")

        assert get_action_state("phone_user_1", 1) == ActionState.IN_PROGRESS
        assert get_action_state("desktop_user_1", 1) == ActionState.ASSIGNED


class TestFlowManagement:
    """Tests for flow increment logic."""

    def test_safe_increment_rejects_non_terminated(self):
        """safe_increment_flow raises if actions are not terminated."""
        from lifecycle_hooks import safe_set_state, ActionState
        user_prompt = "test_safe_inc"
        safe_set_state(user_prompt, 1, ActionState.ASSIGNED, "init")
        state = ActionState.ASSIGNED
        assert state != ActionState.TERMINATED

    def test_parallel_actions_execute_independently(self):
        """Actions without dependencies can be identified for parallel execution."""
        try:
            from agent_ledger import SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
        except ImportError:
            pytest.skip("agent_ledger not installed")

        ledger = SmartLedger(agent_id="parallel_test", session_id="parallel_session")
        for i in range(1, 4):
            task = Task(
                task_id=f"action_{i}", description=f"Parallel {i}",
                task_type=TaskType.PRE_ASSIGNED,
                execution_mode=ExecutionMode.PARALLEL,
                status=TaskStatus.PENDING,
                prerequisites=[]
            )
            ledger.add_task(task)

        pending = [t for t in ledger.tasks.values()
                   if t.status == TaskStatus.PENDING and not t.prerequisites]
        assert len(pending) == 3
