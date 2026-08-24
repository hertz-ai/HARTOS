"""Claude Code as HARTOS's EXPERT-tier inference engine (flow #3, task 43).

Covers the two no-parallel-path guarantees and the resilience contract:
  * ONE invocation primitive, two modes (agentic vs inference).
  * the OpenAI shim returns chat.completion shape on success, and maps Claude
    failures onto the HTTP statuses dispatch.py's EXISTING fallback ladder
    treats as transient (so it degrades to the in-house LLM, no new fallback).
All `claude -p` calls are stubbed — no subprocess, no network.
"""
import json
from unittest.mock import patch

import integrations.coding_agent.claude_code_backend as be


# ─── the shared invocation primitive ─────────────────────────────────────────

def _fake_run(stdout='', stderr='', rc=0):
    class _P:
        returncode = rc
    p = _P()
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_inference_mode_constrains_tools_and_text_output():
    with patch('subprocess.run', return_value=_fake_run('4')) as sr:
        r = be.invoke_claude('2+2?', mode='inference')
    assert r['ok'] and r['stdout'] == '4'
    cmd = sr.call_args[0][0]
    assert '--output-format' in cmd and 'text' in cmd
    assert '--allowedTools' in cmd          # no tools in inference mode


def test_agentic_mode_is_a_plain_run_no_tool_gating():
    with patch('subprocess.run', return_value=_fake_run('done')) as sr:
        r = be.invoke_claude('fix the bug', mode='agentic', cwd='/repo')
    assert r['ok'] and r['returncode'] == 0
    cmd = sr.call_args[0][0]
    assert '--allowedTools' not in cmd       # agentic keeps full tools
    assert sr.call_args[1]['cwd'] == '/repo'


def test_classify_overload_auth_timeout():
    assert be.classify_failure({'ok': False, 'stderr': 'Error 529 overloaded'}) == 'overload'
    assert be.classify_failure({'ok': False, 'stderr': 'Please run /login (unauthorized)'}) == 'auth'
    assert be.classify_failure({'ok': False, 'category': 'timeout'}) == 'timeout'
    assert be.classify_failure({'ok': True}) is None


# ─── the copilot daemon still works, on the shared primitive ─────────────────

def test_daemon_run_claude_delegates_and_preserves_shape():
    import importlib.util, os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        'hart_copilot_daemon', os.path.join(root, 'scripts', 'hart_copilot_daemon.py'))
    dae = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dae)
    with patch('subprocess.run', return_value=_fake_run('ok', 'warn', 0)):
        out = dae.run_claude('do it')
    # same keys the daemon always returned; truncation preserved
    assert out['ok'] is True and out['returncode'] == 0
    assert out['stdout'] == 'ok' and out['stderr'] == 'warn'


# ─── the OpenAI shim ─────────────────────────────────────────────────────────

def _client():
    from flask import Flask
    from integrations.providers.claude_code_endpoint import claude_code_bp
    app = Flask(__name__)
    app.register_blueprint(claude_code_bp)
    return app.test_client()


def test_shim_success_returns_openai_chat_completion():
    with patch('integrations.providers.claude_code_endpoint.invoke_claude',
               return_value={'ok': True, 'returncode': 0, 'stdout': 'the answer', 'stderr': ''}):
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'model': 'claude-code',
                                    'messages': [{'role': 'user', 'content': 'q?'}]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['object'] == 'chat.completion'
    assert body['choices'][0]['message'] == {'role': 'assistant', 'content': 'the answer'}
    assert body['choices'][0]['finish_reason'] == 'stop'


def test_shim_maps_overload_to_503_for_fallback():
    with patch('integrations.providers.claude_code_endpoint.invoke_claude',
               return_value={'ok': False, 'stderr': '529 overloaded'}):
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]})
    # 503 is in dispatch.py's transient set -> circuit breaker + fall to local.
    assert resp.status_code == 503


def test_shim_maps_auth_failure_to_503_degrade_to_local():
    with patch('integrations.providers.claude_code_endpoint.invoke_claude',
               return_value={'ok': False, 'stderr': 'unauthorized: please run /login'}):
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]})
    assert resp.status_code == 503   # a lapsed subscription must not error the OS


def test_shim_maps_timeout_to_504():
    with patch('integrations.providers.claude_code_endpoint.invoke_claude',
               return_value={'ok': False, 'category': 'timeout', 'error': 'timed out'}):
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]})
    assert resp.status_code == 504


def test_shim_flattens_system_and_multiturn():
    captured = {}

    def _cap(prompt, **kw):
        captured['prompt'] = prompt
        captured['system'] = kw.get('system')
        return {'ok': True, 'returncode': 0, 'stdout': 'ok', 'stderr': ''}

    with patch('integrations.providers.claude_code_endpoint.invoke_claude', _cap):
        _client().post('/api/claude/v1/chat/completions', json={'messages': [
            {'role': 'system', 'content': 'be terse'},
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'},
            {'role': 'user', 'content': 'bye'},
        ]})
    assert captured['system'] == 'be terse'
    assert 'bye' in captured['prompt'] and captured['prompt'].strip().endswith('assistant:')
