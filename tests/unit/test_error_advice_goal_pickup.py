"""Regression: error_advice must file an AgentGoal with the EXACT
goal_type + config-key contract that the registered prompt builder
reads.  The cycle-mismatch bug we just hit:

  error_advice.py emitted goal_type='auto_heal_failure' with config
  keys {category, severity, error_type, ...}.

  goal_manager.py:1011 registers ONLY 'self_heal' (no
  'auto_heal_failure'), and goal_manager.py:836-856 reads
  config['exc_type'] / source_module / source_function /
  occurrence_count / sample_traceback.

  Result: the local coding agent never received any of these goals
  because the goal_type was unregistered, and even if it had been
  registered, the prompt would have rendered with 'Unknown' /
  'unknown' / 'N/A' for every field because the keys didn't match.

This test pins both halves of the contract so any future rename
breaks loudly here instead of silently in production.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_self_heal_goal_type_is_registered():
    """The goal_type error_advice writes must be in
    goal_manager._prompt_builders — otherwise no builder fires and
    the goal sits in the queue with no agent ever picking it up."""
    from integrations.agent_engine.goal_manager import _prompt_builders
    assert 'self_heal' in _prompt_builders, (
        "Goal type 'self_heal' missing from _prompt_builders. "
        "If renamed in goal_manager.register_goal_type calls, "
        "core/error_advice.py::_try_agent_remediation must be "
        "updated to emit the new name."
    )


def test_self_heal_prompt_reads_config_keys_error_advice_writes():
    """The keys error_advice puts in config must match what
    _build_self_heal_prompt reads.  Otherwise the prompt renders with
    'Unknown' / 'N/A' placeholders and the coding agent gets a
    contextless goal."""
    from integrations.agent_engine.goal_manager import _build_self_heal_prompt

    # Mirror the config error_advice produces (see
    # core/error_advice.py::_try_agent_remediation)
    fake_goal = {
        'title': 'Self-heal: tts.install (RuntimeError)',
        'description': 'A high-severity tts.install failure escaped...',
        'config': {
            'exc_type':          'RuntimeError',
            'source_module':     '/path/to/foo.py',
            'source_function':   'do_thing',
            'occurrence_count':  1,
            'sample_traceback':  'File "foo.py", line 42, in do_thing\n    raise...',
            # Extra keys (not read by the prompt) — must not break it
            'category':          'tts.install',
            'severity':          'high',
            'error_message':     'something broke',
            'fingerprint':       'RuntimeError:something broke',
            'context':           {'backend': 'chatterbox_turbo'},
        },
    }
    prompt = _build_self_heal_prompt(fake_goal)
    # Each key error_advice writes must surface in the rendered prompt
    assert 'RuntimeError' in prompt, "exc_type not rendered"
    assert '/path/to/foo.py' in prompt, "source_module not rendered"
    assert 'do_thing' in prompt, "source_function not rendered"
    assert 'line 42' in prompt, "sample_traceback not rendered"
    # Should NOT contain the placeholder values (which would mean
    # the prompt builder didn't find the key)
    assert 'Unknown' not in prompt, (
        "Prompt rendered 'Unknown' — exc_type key mismatch"
    )
    assert "Module: unknown" not in prompt, (
        "Prompt rendered 'Module: unknown' — source_module key mismatch"
    )


def test_error_advice_emits_self_heal_with_correct_keys():
    """End-to-end: handle_exception with agent_remediation=True must
    call GoalManager.create_goal with goal_type='self_heal' and the
    config keys _build_self_heal_prompt expects."""
    from core import error_advice as ea

    captured = {}

    def fake_create_goal(db, **kwargs):
        captured.update(kwargs)
        return {'success': True, 'goal_id': 'test'}

    fake_session = MagicMock()
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)

    fake_db_session_fn = MagicMock(return_value=fake_session)

    fake_goal_manager = MagicMock()
    fake_goal_manager.create_goal = fake_create_goal

    fake_models_module = MagicMock()
    fake_models_module.db_session = fake_db_session_fn

    fake_gm_module = MagicMock()
    fake_gm_module.GoalManager = fake_goal_manager

    # error_advice does inline imports inside _try_agent_remediation
    # so we patch sys.modules to intercept them
    with patch.dict(sys.modules, {
        'integrations.agent_engine.goal_manager': fake_gm_module,
        'integrations.social.models': fake_models_module,
    }):
        # Reset throttle so the test always emits
        ea._THROTTLE.clear()
        try:
            raise RuntimeError("install failed: missing transitive")
        except RuntimeError as e:
            ea.handle_exception(
                e,
                category='tts.install',
                severity='high',
                agent_remediation=True,
                context={'backend': 'chatterbox_turbo'},
            )

    # Must have called create_goal exactly once
    assert captured, (
        "GoalManager.create_goal was NEVER called — "
        "agent_remediation=True path didn't fire"
    )
    # MUST be the registered goal_type
    assert captured.get('goal_type') == 'self_heal', (
        f"goal_type must be 'self_heal' (registered in goal_manager.py:1011), "
        f"got {captured.get('goal_type')!r}"
    )
    # Config MUST carry the keys _build_self_heal_prompt reads
    config = captured.get('config') or {}
    required_keys = {
        'exc_type', 'source_module', 'source_function',
        'occurrence_count', 'sample_traceback',
    }
    missing = required_keys - set(config.keys())
    assert not missing, (
        f"Config missing keys _build_self_heal_prompt requires: {missing}. "
        f"_build_self_heal_prompt would render these as 'Unknown'/'N/A' "
        f"and the coding agent would get a contextless goal."
    )
    assert config['exc_type'] == 'RuntimeError'
    assert 'install failed: missing transitive' in (
        config.get('error_message') or ''
    )
