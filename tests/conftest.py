"""Pytest fixtures and configuration for test suite"""
import pytest
import os
import sys
import json
import shutil
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

# ─── Windows excepthook crash guard ───
# On Windows, sys.excepthook can crash when writing tracebacks to certain
# consoles/pipes, killing the entire pytest process mid-run.  Installing a
# resilient hook prevents the abort while still attempting to display the error.
_original_excepthook = sys.excepthook

def _safe_excepthook(exc_type, exc_value, exc_tb):
    try:
        _original_excepthook(exc_type, exc_value, exc_tb)
    except Exception:
        # Fallback: write to stderr directly (avoids "I/O on closed file" abort)
        try:
            sys.stderr.write(f"\n[conftest] Unhandled {exc_type.__name__}: {exc_value}\n")
        except Exception:
            pass

sys.excepthook = _safe_excepthook

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# ─── Exclude standalone scripts that crash pytest collection ───
# These files have sys.exit() at module level or module-level assertions.
# They are standalone test runners, not pytest test files.
# Run them directly with: python tests/standalone/<filename>.py
collect_ignore = [
    os.path.join(os.path.dirname(__file__), 'standalone'),  # entire dir
]
collect_ignore_glob = [
    # runtime_tests/ need a live API server - run via scripts/run_e2e_tests.bat
    os.path.join(os.path.dirname(__file__), 'e2e', 'runtime_tests', '*.py'),
]

from lifecycle_hooks import (
    ActionState, FlowState,
    action_states, flow_lifecycle,
    initialize_deterministic_actions
)
try:
    from helper import Action
except ImportError:
    Action = None  # autogen not installed — tests needing Action will skip


def pytest_configure(config):
    """Register custom markers for optional dependencies."""
    config.addinivalue_line("markers", "requires_pyautogui: test needs pyautogui")
    config.addinivalue_line("markers", "requires_telegram: test needs python-telegram-bot")


@pytest.fixture(autouse=True)
def reset_state_machine():
    """Reset state machine before each test.

    Wrapped in try/except because initialize_deterministic_actions()
    requires Flask app context, which not all test files set up.
    """
    try:
        action_states.clear()
        flow_lifecycle.flows.clear()
        initialize_deterministic_actions()
    except (RuntimeError, Exception):
        pass  # No Flask app context - test doesn't use lifecycle hooks
    yield
    try:
        action_states.clear()
        flow_lifecycle.flows.clear()
    except (RuntimeError, Exception):
        pass


@pytest.fixture
def test_user_prompt():
    """Standard test user prompt"""
    return "test_user_123_prompt_456"


@pytest.fixture
def test_prompt_id():
    """Standard test prompt ID"""
    return 456


@pytest.fixture
def test_user_id():
    """Standard test user ID"""
    return 123


@pytest.fixture
def sample_actions():
    """Sample actions for testing"""
    return [
        {"action": "Create a new file", "description": "Create test.txt"},
        {"action": "Write content", "description": "Write hello world"},
        {"action": "Close file", "description": "Close test.txt"}
    ]


@pytest.fixture
def mock_user_tasks(sample_actions):
    """Mock user tasks object"""
    tasks = Action(sample_actions)
    tasks.current_action = 1
    tasks.fallback = False
    tasks.recipe = False
    return tasks


@pytest.fixture
def mock_group_chat():
    """Mock autogen group chat object"""
    chat = Mock()
    chat.messages = []
    return chat


@pytest.fixture
def mock_agents():
    """Mock autogen agents"""
    return {
        'assistant': Mock(),
        'chat_instructor': Mock(),
        'executor': Mock(),
        'status_verifier': Mock(),
        'helper': Mock(),
        'user': Mock()
    }


@pytest.fixture
def temp_prompts_dir(tmp_path):
    """Create temporary prompts directory"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Change to temp directory
    original_dir = os.getcwd()
    os.chdir(tmp_path)

    yield prompts_dir

    # Restore original directory
    os.chdir(original_dir)


@pytest.fixture
def sample_config_json(temp_prompts_dir, test_prompt_id):
    """Create sample config JSON file"""
    config = {
        "flows": [
            {
                "persona": "Test Assistant",
                "actions": [
                    {"action": "Create file", "description": "Create test.txt"},
                    {"action": "Write content", "description": "Write hello"}
                ]
            },
            {
                "persona": "Test Reviewer",
                "actions": [
                    {"action": "Review file", "description": "Review test.txt"}
                ]
            }
        ]
    }

    config_file = temp_prompts_dir / f"{test_prompt_id}.json"
    with open(config_file, 'w') as f:
        json.dump(config, f)

    return config


@pytest.fixture
def sample_recipe_json(temp_prompts_dir, test_prompt_id):
    """Create sample recipe JSON file"""
    recipe = {
        "actions": [
            {
                "action_id": 1,
                "action": "Create file",
                "recipe": [
                    {
                        "steps": "Open file",
                        "tool_name": "file_tool",
                        "generalized_functions": "open('test.txt', 'w')"
                    }
                ]
            }
        ],
        "scheduled_tasks": []
    }

    recipe_file = temp_prompts_dir / f"{test_prompt_id}_0_recipe.json"
    with open(recipe_file, 'w') as f:
        json.dump(recipe, f)

    return recipe


@pytest.fixture
def mock_flask_app():
    """Provide a real Flask application context.

    Using patch('flask.current_app') fails on Python 3.10+ because
    mock introspects the werkzeug LocalProxy (calls hasattr(__func__))
    which triggers RuntimeError outside an app context.
    A real minimal Flask app avoids this entirely.
    """
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def mock_database_requests():
    """Mock database HTTP requests"""
    with patch('requests.patch') as mock_patch, \
         patch('requests.get') as mock_get, \
         patch('requests.post') as mock_post:

        mock_patch.return_value.status_code = 200
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {}
        mock_post.return_value.status_code = 200

        yield {
            'patch': mock_patch,
            'get': mock_get,
            'post': mock_post
        }


@pytest.fixture
def mock_crossbar_client():
    """Mock Crossbar HTTP client"""
    with patch('create_recipe.client') as mock_client:
        mock_client.publish = Mock()
        yield mock_client


class MockMessage:
    """Mock message object for group chat"""
    def __init__(self, content, name='TestAgent', role='assistant'):
        self.content = content
        self.name = name
        self.role = role

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.fixture
def create_mock_message():
    """Factory fixture for creating mock messages"""
    def _create_message(content, name='TestAgent', role='assistant'):
        return {
            'content': content,
            'name': name,
            'role': role
        }
    return _create_message


@pytest.fixture
def action_flow_scenarios():
    """Predefined action flow scenarios for testing"""
    return {
        'success_flow': [
            ActionState.ASSIGNED,
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED,
            ActionState.FALLBACK_REQUESTED,
            ActionState.FALLBACK_RECEIVED,
            ActionState.RECIPE_REQUESTED,
            ActionState.RECIPE_RECEIVED,
            ActionState.TERMINATED
        ],
        'error_retry_flow': [
            ActionState.ASSIGNED,
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.ERROR,
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.COMPLETED
        ],
        'pending_completion_flow': [
            ActionState.ASSIGNED,
            ActionState.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED,
            ActionState.PENDING,
            ActionState.COMPLETED
        ]
    }
