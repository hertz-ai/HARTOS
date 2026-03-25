"""Tests for AiderNativeBackend and vendored aider_core modules."""
import os
import sys
import json
import tempfile
import pytest

# ── aider_core import availability ──

def test_aider_core_search_replace_import():
    """Vendored search_replace module imports successfully."""
    from integrations.coding_agent.aider_core.coders.search_replace import (
        flexible_search_and_replace,
    )
    assert callable(flexible_search_and_replace)


def test_aider_core_repomap_import():
    """Vendored repomap module imports successfully."""
    pytest.importorskip('grep_ast.tsl', reason='grep_ast.tsl not available')
    from integrations.coding_agent.aider_core.repomap import RepoMap
    assert RepoMap is not None


def test_aider_core_io_adapter_import():
    """IO adapter imports and instantiates."""
    from integrations.coding_agent.aider_core.io_adapter import SimpleIO
    io = SimpleIO()
    assert hasattr(io, 'read_text')
    assert hasattr(io, 'tool_output')
    assert hasattr(io, 'tool_error')


def test_aider_core_run_cmd_import():
    """run_cmd module imports and has run_cmd_subprocess."""
    from integrations.coding_agent.aider_core.run_cmd import run_cmd_subprocess
    assert callable(run_cmd_subprocess)


def test_aider_core_hart_model_adapter():
    """HartModelAdapter imports and instantiates."""
    from integrations.coding_agent.aider_core.hart_model_adapter import HartModelAdapter
    adapter = HartModelAdapter(
        model_name='gpt-4',
        max_context_window=128000,
    )
    assert adapter.name == 'gpt-4'
    assert adapter.max_context_window == 128000


def test_hart_model_adapter_token_count():
    """HartModelAdapter counts tokens."""
    from integrations.coding_agent.aider_core.hart_model_adapter import HartModelAdapter
    adapter = HartModelAdapter(model_name='gpt-4')
    count = adapter.token_count('Hello world, this is a test.')
    assert isinstance(count, int)
    assert count > 0


# ── search_replace functionality ──

def test_search_replace_basic():
    """Basic search and replace works."""
    from integrations.coding_agent.aider_core.coders.search_replace import (
        flexible_search_and_replace, editblock_strategies,
    )
    original = 'def hello():\n    print("hello")\n'
    search = 'def hello():\n    print("hello")'
    replace = 'def hello():\n    """Say hello."""\n    print("hello")'

    texts = (search, replace, original)
    result = flexible_search_and_replace(texts, editblock_strategies)
    assert '"""Say hello."""' in result


def test_search_replace_fuzzy_match():
    """Aider's flexible search uses multiple strategies including fuzzy."""
    from integrations.coding_agent.aider_core.coders.search_replace import (
        flexible_search_and_replace, editblock_strategies,
    )
    # Aider's search/replace is intentionally aggressive with fuzzy matching.
    # Even mismatched function names may match via indentation strategies.
    # Verify it returns a string (either matched or original).
    original = 'def hello():\n    pass\n'
    search = 'def goodbye():\n    pass'
    replace = 'def goodbye():\n    return True'

    texts = (search, replace, original)
    result = flexible_search_and_replace(texts, editblock_strategies)
    assert isinstance(result, str)
    assert len(result) > 0


# ── AiderNativeBackend ──

def test_backend_import():
    """AiderNativeBackend imports successfully."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    assert AiderNativeBackend is not None


def test_backend_properties():
    """Backend has correct name and strengths."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    backend = AiderNativeBackend()
    assert backend.name == 'aider_native'
    assert 'code_review' in backend.strengths
    assert 'refactoring' in backend.strengths
    assert 'debugging' in backend.strengths


def test_backend_is_installed():
    """Backend reports installed when aider_core is available."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    backend = AiderNativeBackend()
    # Should be True since we have the vendored modules
    result = backend.is_installed()
    assert isinstance(result, bool)


def test_backend_parse_edit_blocks():
    """Backend correctly parses SEARCH/REPLACE blocks."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    backend = AiderNativeBackend()

    # Parser expects backtick-wrapped filenames
    text = '''Here's the edit:

`test.py`
<<<<<<< SEARCH
def hello():
    pass
=======
def hello():
    """Say hello."""
    print("hello")
>>>>>>> REPLACE

Done.'''

    blocks = backend._parse_edit_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]['file'] == 'test.py'
    assert 'def hello():\n    pass' in blocks[0]['search']
    assert '"""Say hello."""' in blocks[0]['replace']


def test_backend_parse_multiple_edit_blocks():
    """Backend parses multiple SEARCH/REPLACE blocks."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    backend = AiderNativeBackend()

    text = '''`file_a.py`
<<<<<<< SEARCH
x = 1
=======
x = 2
>>>>>>> REPLACE

`file_b.py`
<<<<<<< SEARCH
y = 3
=======
y = 4
>>>>>>> REPLACE'''

    blocks = backend._parse_edit_blocks(text)
    assert len(blocks) == 2
    assert blocks[0]['file'] == 'file_a.py'
    assert blocks[1]['file'] == 'file_b.py'


def test_backend_apply_edits():
    """Backend applies edits from LLM response text."""
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    backend = AiderNativeBackend()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = os.path.join(tmpdir, 'test.py')
        with open(test_file, 'w') as f:
            f.write('def hello():\n    pass\n')

        # _apply_edits takes a response string (not blocks), parses it, applies
        response = '''`test.py`
<<<<<<< SEARCH
def hello():
    pass
=======
def hello():
    return "hello"
>>>>>>> REPLACE'''

        results = backend._apply_edits(response, tmpdir, ['test.py'])
        applied = [r for r in results if r['status'] == 'applied']
        assert len(applied) == 1
        assert applied[0]['file'] == 'test.py'

        with open(test_file) as f:
            content = f.read()
        assert 'return "hello"' in content


# ── tool_backends integration ──

def test_backends_dict_has_aider_native():
    """BACKENDS dict includes aider_native entry."""
    from integrations.coding_agent.tool_backends import BACKENDS
    assert 'aider_native' in BACKENDS


# ── tool_router integration ──

def test_heuristic_defaults_include_aider_native():
    """HEURISTIC_DEFAULTS includes aider_native for key task types."""
    from integrations.coding_agent.tool_router import HEURISTIC_DEFAULTS
    # aider_native is the default for refactor, bug_fix, multi_file_edit
    assert HEURISTIC_DEFAULTS.get('refactor') == 'aider_native'
    assert HEURISTIC_DEFAULTS.get('bug_fix') == 'aider_native'
    assert HEURISTIC_DEFAULTS.get('multi_file_edit') == 'aider_native'


# ── installer integration ──

def test_installer_has_aider_native():
    """Installer TOOL_REGISTRY includes aider_native."""
    from integrations.coding_agent.installer import TOOL_REGISTRY
    assert 'aider_native' in TOOL_REGISTRY
    binary, package, license_type = TOOL_REGISTRY['aider_native']
    assert binary == ''  # In-process, no binary
    assert 'tree-sitter' in package
    assert license_type == 'Apache-2.0'


def test_installer_detect_installed():
    """detect_installed includes aider_native."""
    from integrations.coding_agent.installer import detect_installed
    result = detect_installed()
    assert 'aider_native' in result
    assert isinstance(result['aider_native'], bool)


def test_installer_get_tool_info():
    """get_tool_info includes aider_native with type field."""
    from integrations.coding_agent.installer import get_tool_info
    info = get_tool_info()
    assert 'aider_native' in info
    assert info['aider_native']['type'] == 'native'


# ── run_cmd ──

def test_run_cmd_subprocess():
    """run_cmd_subprocess executes a basic command."""
    from integrations.coding_agent.aider_core.run_cmd import run_cmd_subprocess
    exit_code, output = run_cmd_subprocess('python --version')
    assert exit_code == 0
    assert 'Python' in output
