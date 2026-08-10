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


def refuse_all_network(mp, reason='test suite: no peer services'):
    """Patch requests + httpx to refuse instantly instead of dialing a real
    socket. The ONE reusable network seal — a suite that wants hermetic
    isolation installs it via a scoped fixture (see tests/e2e/conftest.py).

    Why it matters beyond speed: a pooled requests.Session or the shared LLM
    httpx.Client established by one test and reused by a LATER test — after the
    target is gone — blackholes on recv until the pytest timeout, and the two
    tests pass alone but hang in sequence (cross-test socket pollution). This
    caps it at the class level so no test can open a real pooled connection
    that pollutes another. Handlers hit the exact same ConnectionError degrade
    path they take in CI (which has no network), just deterministically and in
    microseconds.

    `mp` is a pytest MonkeyPatch (fixture-provided or a manual instance);
    caller owns undo(). The shell_surface conftest predates this and keeps its
    own inline copy for now — a later DRY pass folds it onto this helper.
    """
    def _refuse(*a, **kw):
        import requests as _rq
        raise _rq.exceptions.ConnectionError(reason)

    try:
        import requests
        for name in ('request', 'get', 'post', 'put', 'delete', 'patch', 'head'):
            mp.setattr(requests, name, _refuse, raising=False)
        # Session.request is the pooled path (core.http_pool) — class-level so
        # every Session instance, including the shared pool, is covered.
        mp.setattr(requests.Session, 'request', _refuse, raising=False)
    except ImportError:
        pass
    try:
        import httpx

        def _refuse_httpx(*a, **kw):
            raise httpx.ConnectError(reason)
        for name in ('request', 'get', 'post', 'put', 'delete'):
            mp.setattr(httpx, name, _refuse_httpx, raising=False)
        # Client.request covers the shared autogen/openai LLM client.
        mp.setattr(httpx.Client, 'request', _refuse_httpx, raising=False)
    except ImportError:
        pass

    # The bulletproof floor: refuse OUTBOUND TCP at socket.create_connection.
    # The high-level patches above miss anything that reaches the network by a
    # path they don't name — the pooled httpx LLM client funnels through
    # httpcore's transport, NOT Client.request, so a cross-test-polluted
    # connection still blackholed on recv despite the httpx patch.
    # create_connection is the ONE call every stdlib-based outbound dialer
    # (httpcore's sync backend, urllib3/requests) goes through.
    #
    # ONLY create_connection — NOT socket.socket.connect. Patching the raw
    # method breaks asyncio's event loop, which connects an internal self-pipe
    # socket at startup (ProactorEventLoop._ssock on Windows / the self-pipe on
    # Unix); an over-broad seal there turns every async test into an
    # AttributeError. create_connection is outbound-client-only and asyncio
    # does not use it for the self-pipe, so this seals peer dialing without
    # touching the loop, and leaves socket()/bind()/listen()/accept() alone so
    # a LOCAL server test still works.
    import socket as _socket

    def _refuse_connect(*a, **kw):
        raise ConnectionError(reason)

    mp.setattr(_socket, 'create_connection', _refuse_connect, raising=False)

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

# #163 — heavy imports are LAZY (done inside the fixtures that use them) so
# importing this conftest is cheap and does NOT pull the full dependency chain
# at collection time: lifecycle_hooks → core.session_cache → core.http_pool →
# requests, and helper → langchain. That heavy module-level import is the ONLY
# reason CI ran `--noconftest` (copied from the deps-light nix-check context) —
# which then skipped these fixtures + the autouse FSM reset, so the recipe/
# action-execution tests ERRORed instead of running. Lazy here = conftest loads
# everywhere, and `--noconftest` can be dropped (proven: those tests go from 40
# fixture-not-found errors to 48 passing).


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
        from lifecycle_hooks import (
            action_states, flow_lifecycle, initialize_deterministic_actions)
        action_states.clear()
        flow_lifecycle.flows.clear()
        initialize_deterministic_actions()
    except (RuntimeError, Exception):
        pass  # No Flask app context / deps absent - test doesn't use lifecycle
    yield
    try:
        from lifecycle_hooks import action_states, flow_lifecycle
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
    try:
        from helper import Action
    except ImportError:
        pytest.skip("helper.Action unavailable (autogen not installed)")
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
    """Create temporary prompts directory and patch PROMPTS_DIR in all modules."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    prompts_str = str(prompts_dir)
    original_dir = os.getcwd()
    os.chdir(tmp_path)

    # Patch PROMPTS_DIR in all modules that reference it
    patches = []
    for mod_name in ('create_recipe', 'reuse_recipe', 'helper', 'recipe_experience'):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, 'PROMPTS_DIR'):
            p = patch.object(mod, 'PROMPTS_DIR', prompts_str)
            p.start()
            patches.append(p)

    yield prompts_dir

    for p in patches:
        p.stop()
    os.chdir(original_dir)


@pytest.fixture
def sample_config_json(temp_prompts_dir, test_prompt_id):
    """Create sample config JSON file"""
    config = {
        "personas": [
            {"name": "Test Assistant"},
            {"name": "Test Reviewer"}
        ],
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
    from lifecycle_hooks import ActionState
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


# ═══════════════════════════════════════════════════════════════════════════
# The interpreter MUST exit. (task #37)
# ═══════════════════════════════════════════════════════════════════════════
# Measured on CI run 30885611303, shard 0:
#
#     06:56:23  step starts, 76 files
#     06:58:23  pytest PRINTS ITS SUMMARY - 1519 passed in 117.86s
#               <- nothing at all for 1h55m ->
#     08:53     the 120-minute job cap fires, reported as "cancelled"
#               "Terminate orphan process: pid (5652) (pt_main_thread)"
#
# The tests take TWO MINUTES. Everything after is Python's threading._shutdown()
# joining a non-daemon thread that never returns, so the process never exits.
# FIVE of eight shards burned the full cap that way and reported NOTHING — which
# is the real reason this gate has never produced a full verdict. It was never
# the volume of tests, and neither raising the cap nor adding shards could have
# helped; both treat a hang as slowness.
#
# `--timeout` cannot see it: that is PER TEST, and this happens after the last
# test has finished.
#
# Two jobs here, in order:
#   1. NAME the leaker. A hang with no attribution costs a full CI round to
#      learn nothing, which is exactly what happened for days.
#   2. GUARANTEE termination, so a leak degrades to a named warning instead of
#      a two-hour silence. os._exit skips interpreter shutdown — and therefore
#      the very join that hangs — while preserving pytest's exit status, so the
#      gate's verdict is unchanged.
#
# Deliberately conditional: if nothing is leaking, this does nothing at all and
# the normal shutdown runs (atexit handlers, coverage flush, everything). The
# hard exit happens ONLY in the case that would otherwise hang forever, where
# the alternative is not "clean shutdown" but "no result at all".

def pytest_sessionfinish(session, exitstatus):
    """Report any non-daemon thread still alive, then guarantee we exit."""
    import os
    import sys
    import threading

    alive = [t for t in threading.enumerate()
             if t is not threading.main_thread()
             and t.is_alive() and not t.daemon]
    if not alive:
        return                      # normal shutdown — untouched

    import sys
    print("\n" + "=" * 74, file=sys.stderr)
    print("NON-DAEMON THREADS STILL ALIVE at session end:", file=sys.stderr)
    print("(reported BEFORE atexit runs, so a pool with a registered "
          "shutdown may still clear on its own — the blocked frame below is "
          "what tells you which.)", file=sys.stderr)
    for t in alive:
        print(f"  - {t.name!r}  target={getattr(t, '_target', None)!r}",
              file=sys.stderr)
    try:
        frames = sys._current_frames()
        for t in alive:
            f = frames.get(t.ident)
            if f is not None:
                print(f"  {t.name} is blocked at "
                      f"{f.f_code.co_filename}:{f.f_lineno} "
                      f"in {f.f_code.co_name}", file=sys.stderr)
    except Exception as exc:        # never let diagnostics break the exit
        print(f"  (could not read frames: {exc})", file=sys.stderr)
    print("A thread idle in _worker is an executor awaiting shutdown; one "
          "blocked ELSEWHERE is running work that never returns. Forcing exit "
          "so the gate reports instead of hanging.", file=sys.stderr)
    print("=" * 74, file=sys.stderr)
    sys.stderr.flush()
    sys.stdout.flush()

    # FORCE EXIT ONLY FOR THREADS THAT WOULD ACTUALLY HANG.
    #
    # An executor worker parked in concurrent/futures/thread.py::_worker is
    # IDLE, waiting on its work queue — and concurrent.futures registers its own
    # atexit hook that wakes every such worker before joining it. Those always
    # clear on their own, so killing the process for them would skip atexit and
    # the coverage flush for no reason. The first version of this hook did
    # exactly that, and the evidence was in its own output: uctx-refresh_0/_1
    # sitting in _worker on a run that had no hang at all.
    #
    # A thread blocked ANYWHERE ELSE is running work that never returns. That is
    # the one that hangs, and the one worth dying for.
    blocking = []
    try:
        frames = sys._current_frames()
        for t in alive:
            f = frames.get(t.ident)
            if f is None or os.path.basename(f.f_code.co_filename) != 'thread.py'                     or f.f_code.co_name != '_worker':
                blocking.append(t)
    except Exception:
        blocking = alive          # cannot tell -> assume the worst, still exit

    if not blocking:
        print("All of the above are IDLE executor workers; concurrent.futures "
              "wakes those at exit. Leaving shutdown alone.", file=sys.stderr)
        sys.stderr.flush()
        return

    print("Blocking (NOT idle): "
          + ", ".join(repr(t.name) for t in blocking), file=sys.stderr)
    sys.stderr.flush()
    os._exit(exitstatus)
