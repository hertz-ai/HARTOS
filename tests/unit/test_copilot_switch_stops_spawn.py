"""Switching the copilot off stops `claude -p` from being spawned at all.

Measured 2026-09-16 on the bundled desktop, before this fix: the owner
turned the copilot off in Admin at 15:53:36 ("copilot switched off" logged),
and in the six minutes after it 16 new expert-tier sessions were written and
two `claude.EXE -p` children of Nunba.exe were live.  The switch gated
claude_code_available(), which only the boot-time registration and GET
/models consulted; the request handler that spawns the process consulted
nothing, and the registered backend was never re-evaluated.

The contract this pins:

  * invoke_claude, the ONE `claude -p` call site, refuses when the switch is
    off -- for both consumers (the expert shim and the copilot daemon), with
    no subprocess spawned.
  * the shim maps that refusal onto 503, the rung dispatch.py's ladder reads
    as "degrade to local", so a switched-off copilot never errors the OS.
  * the switch itself keeps the expert registry honest: OFF unregisters the
    claude-code backend, so selectors stop offering it and
    get_escalation_expert() answers None; ON registers it back.

    python -m pytest tests/unit/test_copilot_switch_stops_spawn.py -q
"""
import importlib.util
import os
import tempfile

os.environ['CLAUDE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='copilot_switch_')

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

import integrations.coding_agent.claude_code_backend as cc  # noqa: E402
import integrations.agent_engine.model_registry as mr  # noqa: E402


@pytest.fixture(autouse=True)
def _switch(monkeypatch):
    """The marker decides (no env pin); the node LOOKS logged in with a
    resolvable binary, so availability turns on the switch alone."""
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'test-token')
    monkeypatch.setattr(cc, '_resolve_claude_bin', lambda: '/usr/bin/claude')
    monkeypatch.setattr(mr, '_own_llm_target', lambda: ('127.0.0.1', 8082, 'local'))
    cc.set_copilot_enabled(True)
    yield
    cc.set_copilot_enabled(True)


def _fake_run(stdout='ok'):
    class _P:
        returncode = 0
    p = _P()
    p.stdout, p.stderr = stdout, ''
    return p


# ── the spawn primitive ──────────────────────────────────────────────────────

@pytest.mark.parametrize('mode', ['inference', 'agentic'])
def test_off_refuses_at_the_spawn_and_starts_no_process(mode):
    cc.set_copilot_enabled(False)
    with patch('subprocess.run', return_value=_fake_run()) as sr:
        r = cc.invoke_claude('2+2?', mode=mode)
    sr.assert_not_called()
    assert r['ok'] is False and r['category'] == 'off'
    assert 'switched off' in r['error']


def test_on_spawns_as_before():
    with patch('subprocess.run', return_value=_fake_run('4')) as sr:
        r = cc.invoke_claude('2+2?', mode='inference')
    sr.assert_called_once()
    assert r['ok'] and r['stdout'] == '4'


def test_the_env_pin_on_wins_over_the_marker(monkeypatch):
    cc.set_copilot_enabled(False)
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '1')
    with patch('subprocess.run', return_value=_fake_run()) as sr:
        r = cc.invoke_claude('q', mode='inference')
    sr.assert_called_once()
    assert r['ok']


def test_off_classifies_as_its_own_category():
    assert cc.classify_failure({'ok': False, 'category': 'off',
                                'error': 'switched off'}) == 'off'


# ── the expert shim degrades, it does not error ─────────────────────────────

def _client():
    from flask import Flask
    from integrations.providers.claude_code_endpoint import claude_code_bp
    app = Flask(__name__)
    app.register_blueprint(claude_code_bp)
    return app.test_client()


def test_shim_answers_503_with_no_process_when_off():
    cc.set_copilot_enabled(False)
    with patch('subprocess.run', return_value=_fake_run()) as sr:
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]})
    sr.assert_not_called()
    assert resp.status_code == 503          # dispatch.py's "degrade to local" rung
    assert resp.get_json()['error']['category'] == 'off'


# ── the copilot daemon's agentic runs are the same copilot ──────────────────

def test_daemon_run_claude_reports_the_switch_and_spawns_nothing():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        'hart_copilot_daemon', os.path.join(root, 'scripts', 'hart_copilot_daemon.py'))
    dae = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dae)
    cc.set_copilot_enabled(False)
    with patch('subprocess.run', return_value=_fake_run()) as sr:
        out = dae.run_claude('do it')
    sr.assert_not_called()
    assert out['ok'] is False and 'switched off' in out['error']


# ── the offer follows the switch ────────────────────────────────────────────

def test_off_unregisters_the_expert_backend_and_on_registers_it_back():
    assert mr.ensure_claude_code_registered() is True
    assert mr.model_registry.get_model('claude-code') is not None

    cc.set_copilot_enabled(False)
    assert mr.model_registry.get_model('claude-code') is None
    assert mr.model_registry.get_escalation_expert() is None
    assert mr.ensure_claude_code_registered() is False   # stays off while off

    cc.set_copilot_enabled(True)
    assert mr.model_registry.get_model('claude-code') is not None


def test_a_logged_out_node_never_registers_and_off_is_a_no_op(monkeypatch):
    monkeypatch.delenv('CLAUDE_CODE_OAUTH_TOKEN', raising=False)
    monkeypatch.setattr(cc, '_resolve_claude_bin', lambda: '')
    mr.model_registry.unregister('claude-code')
    assert mr.ensure_claude_code_registered() is False
    cc.set_copilot_enabled(False)
    assert mr.model_registry.get_model('claude-code') is None
