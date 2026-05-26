"""Tests for integrations.coding_agent.backend_repair_tools.

Guards:
  1. ``BACKEND_REPAIR_TOOLS`` shape matches the registration contract
     (single dict with name/func/description/tags) so the MCP bridge
     loop in ``mcp_http_bridge.py`` registers it cleanly.
  2. ``repair_backend_venv`` validates the backend name against
     ``ENGINE_REGISTRY`` BEFORE touching the filesystem (no
     path-traversal, no arbitrary-name pip-install).
  3. The graceful-fallback contract holds: when Nunba's tts modules
     are unimportable (source-mode HARTOS) the tool returns a
     ``success=False`` JSON instead of crashing the autogen group.
  4. ``_build_self_heal_prompt`` branches on category + context.backend
     so TTS / subprocess install failures get the repair-tool guidance
     and other categories keep the original generic instructions.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── Tool-list registration contract ─────────────────────────────


def test_backend_repair_tools_list_shape():
    from integrations.coding_agent.backend_repair_tools import (
        BACKEND_REPAIR_TOOLS,
    )

    assert isinstance(BACKEND_REPAIR_TOOLS, list)
    assert len(BACKEND_REPAIR_TOOLS) == 1, (
        'BACKEND_REPAIR_TOOLS should expose exactly one tool today; '
        'when more are added, update this guard.'
    )
    tool = BACKEND_REPAIR_TOOLS[0]
    for key in ('name', 'func', 'description', 'tags'):
        assert key in tool, (
            f"BACKEND_REPAIR_TOOLS[0] missing {key!r} — breaks the "
            f"shape that mcp_http_bridge._register_tool_module reads."
        )
    assert tool['name'] == 'repair_backend_venv'
    assert callable(tool['func'])
    assert 'self_heal' in tool['tags']


def test_repair_backend_venv_callable_signature():
    from integrations.coding_agent.backend_repair_tools import (
        repair_backend_venv,
    )

    import inspect
    sig = inspect.signature(repair_backend_venv)
    params = sig.parameters
    assert 'backend_name' in params, (
        'repair_backend_venv must accept backend_name kwarg'
    )
    assert 'wipe_first' in params, (
        'repair_backend_venv must accept wipe_first kwarg'
    )
    assert params['wipe_first'].default is False


# ── Validation gate ─────────────────────────────────────────────


def test_repair_backend_venv_unknown_backend_returns_error_json():
    """Unknown backend names must not touch filesystem or call pip."""
    from integrations.coding_agent.backend_repair_tools import (
        repair_backend_venv,
    )

    out = repair_backend_venv('not_a_real_backend_xyz')
    payload = json.loads(out)
    assert payload['success'] is False
    assert payload['backend'] == 'not_a_real_backend_xyz'
    assert payload['wiped'] is False
    msg_lower = payload['message'].lower()
    assert 'unknown' in msg_lower or 'unavailable' in msg_lower, (
        f'Expected "unknown" or "unavailable" in message; got {payload["message"]!r}'
    )


def test_repair_backend_venv_engine_registry_unavailable(monkeypatch):
    """When ENGINE_REGISTRY can't be loaded, fail closed with a
    diagnostic message — never let an unvalidated string through."""
    from integrations.coding_agent import backend_repair_tools

    def _explode():
        raise RuntimeError('simulated import failure')

    monkeypatch.setattr(backend_repair_tools, '_get_known_backends',
                        lambda: set())

    out = backend_repair_tools.repair_backend_venv('indic_parler')
    payload = json.loads(out)
    assert payload['success'] is False
    assert 'unavailable' in payload['message'].lower()


def test_repair_backend_venv_known_backend_routed_to_install(monkeypatch):
    """A known backend short-circuits validation and reaches the
    install_backend_full call site (or its ImportError fallback)."""
    from integrations.coding_agent import backend_repair_tools

    monkeypatch.setattr(backend_repair_tools, '_get_known_backends',
                        lambda: {'piper', 'indic_parler', 'kokoro'})

    out = backend_repair_tools.repair_backend_venv('piper')
    payload = json.loads(out)
    # In test env (no Nunba freeze on path) the lazy ImportError
    # fallback fires — confirms the tool reached past validation.
    # If Nunba IS importable here, install_backend_full's outcome is
    # the success bit — accept either branch as long as we got past
    # the unknown-backend gate.
    assert payload['backend'] == 'piper'
    assert 'unknown' not in payload['message'].lower()


# ── Bundled-mode-only graceful fallback ─────────────────────────


def test_repair_backend_venv_source_mode_returns_graceful_message(
    monkeypatch,
):
    """When ``tts.backend_venv`` / ``tts.package_installer`` are not
    importable (the HARTOS-only checkout case), the tool must return a
    structured JSON message — NOT raise — so the autogen group can
    decide what to do."""
    from integrations.coding_agent import backend_repair_tools

    monkeypatch.setattr(backend_repair_tools, '_get_known_backends',
                        lambda: {'indic_parler'})

    # Block imports of the Nunba modules to simulate source-mode HARTOS.
    real_import = __builtins__['__import__'] if isinstance(
        __builtins__, dict) else __builtins__.__import__

    def _blocking_import(name, globals=None, locals=None, fromlist=(),
                         level=0):
        if name.startswith('tts.backend_venv') or \
                name.startswith('tts.package_installer'):
            raise ImportError(f'simulated: {name} not in source mode')
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(
        'builtins.__import__', _blocking_import,
    )

    out = backend_repair_tools.repair_backend_venv('indic_parler')
    payload = json.loads(out)
    assert payload['success'] is False
    assert 'bundled' in payload['message'].lower()
    assert payload['wiped'] is False


# ── Prompt branching ────────────────────────────────────────────


def test_self_heal_prompt_tts_probe_with_backend_mentions_repair_tool():
    """category=tts.probe + context.backend → prompt steers the agent
    to repair_backend_venv FIRST before source-edit."""
    from integrations.agent_engine.goal_manager import (
        _build_self_heal_prompt,
    )

    goal = {
        'title': 'Self-heal: tts.probe (RuntimeError)',
        'description': 'Probe failure',
        'config': {
            'exc_type': 'RuntimeError',
            'category': 'tts.probe',
            'context': {'backend': 'indic_parler'},
            'sample_traceback': '',
            'occurrence_count': 1,
        },
    }
    prompt = _build_self_heal_prompt(goal)
    assert 'repair_backend_venv' in prompt, (
        'tts.probe + backend should surface the repair tool'
    )
    assert 'indic_parler' in prompt, (
        'backend name must appear in the prompt so the agent knows '
        'which backend to call repair_backend_venv with'
    )
    assert 'wipe_first=True' in prompt, (
        'the prompt should mention the escalation path '
        '(wipe_first=True for full clean reinstall)'
    )


def test_self_heal_prompt_subprocess_tool_load_routes_to_repair():
    """category=subprocess.tool_load is the worker-startup-failure
    shape — same fix path."""
    from integrations.agent_engine.goal_manager import (
        _build_self_heal_prompt,
    )

    goal = {
        'title': 'Self-heal: subprocess.tool_load',
        'description': '',
        'config': {
            'exc_type': 'RuntimeError',
            'category': 'subprocess.tool_load',
            'context': {'backend': 'chatterbox_turbo'},
            'sample_traceback': '',
        },
    }
    prompt = _build_self_heal_prompt(goal)
    assert 'repair_backend_venv' in prompt
    assert 'chatterbox_turbo' in prompt


def test_self_heal_prompt_unknown_category_uses_generic_path():
    """Goals from non-TTS failure categories must keep the original
    'read source, write fix' guidance — no false steer toward a tool
    that isn't relevant."""
    from integrations.agent_engine.goal_manager import (
        _build_self_heal_prompt,
    )

    goal = {
        'title': 'Self-heal: random.api_call_failed',
        'description': '',
        'config': {
            'exc_type': 'ConnectionError',
            'category': 'random.api_call_failed',
            'context': {},
        },
    }
    prompt = _build_self_heal_prompt(goal)
    assert 'repair_backend_venv' not in prompt, (
        'unrelated categories must not get repair_backend_venv guidance'
    )
    assert 'Read the source file' in prompt or 'minimal fix' in prompt


def test_self_heal_prompt_tts_probe_no_backend_uses_generic_path():
    """If the producer didn't capture context.backend, the prompt
    can't suggest repair_backend_venv (it has no name to pass).
    Falls back to the generic path."""
    from integrations.agent_engine.goal_manager import (
        _build_self_heal_prompt,
    )

    goal = {
        'title': 'Self-heal: tts.probe',
        'description': '',
        'config': {
            'exc_type': 'RuntimeError',
            'category': 'tts.probe',
            'context': {},  # no backend captured
        },
    }
    prompt = _build_self_heal_prompt(goal)
    assert 'repair_backend_venv' not in prompt, (
        'no backend means no callable suggestion — should fall through'
    )


# ── MCP bridge registration coverage ─────────────────────────────


def test_mcp_bridge_registers_backend_repair_tool():
    """Wires the new module into the bridge's loader loop."""
    from integrations.mcp import mcp_http_bridge

    # Reset bridge state then trigger load.
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()
    mcp_http_bridge._load_tools()

    names = {t['name'] for t in mcp_http_bridge._local_tools}
    assert 'repair_backend_venv' in names, (
        'mcp_http_bridge._load_tools should register '
        'BACKEND_REPAIR_TOOLS via the module-list registration loop'
    )

    # Cleanup.
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
