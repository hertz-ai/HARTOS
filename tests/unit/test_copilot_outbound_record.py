"""The copilot's egress is inspected: every `claude -p` goes through the ONE
outbound record, the ONE DLP scrub, and the ONE provider breaker.

Measured 2026-09-16 on the installed desktop: llm_outbound.jsonl carried 759
records for the day, every one model='local' -- the llama calls the httpx
hook captures -- and zero for the expert tier's claude-code path.  The hook
records only POSTs to the llama ports (core.llm_outbound_logger.
_is_target_request); the shim's POST /api/claude/v1/chat/completions fell
to the "hosted provider" passthrough, which feeds the breaker and writes
nothing; and the `claude -p` subprocess is not HTTP at all.  So the expert
prompt (system text, the conversation, tool results) left the device with no
local record, no redaction, and the breaker blind to a lapsed login.

The contract this pins, at invoke_claude (the one spawn site, both consumers):

  * one llm_outbound.jsonl record per call, source 'claude-code', an
    OpenAI-shaped body so the record's body policy applies uniformly, the
    outcome as response_status (exit code, or the failure category);
  * the prompt and system text are scrubbed by security.dlp_engine before
    they leave; the local record keeps the RAW text, as the llama records
    on the same disk do (owner, 2026-09-16: a raw record on the owner's own
    machine is never wrong -- the scrub is for what leaves), marked
    egress='dlp-scrubbed'; no redactor means no egress -- the OS never
    sends raw text off-device because a module failed to import;
  * an auth failure ('please run /login') feeds the provider breaker under
    the key 'claude-code'; at threshold the shim answers 503 without spawning
    and reads the breaker's state() non-consumingly, so the one real call
    after cooldown resolves the half-open probe;
  * the shim binds the caller's X-HARTOS-Request-ID so the record joins the
    goal's other outbound records.

    python -m pytest tests/unit/test_copilot_outbound_record.py -q
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

import core.llm_outbound_logger as outbound
import integrations.coding_agent.claude_code_backend as cc
from core.circuit_breaker import CircuitState, llm_provider_breaker


@pytest.fixture(autouse=True)
def _desktop(monkeypatch, tmp_path):
    """The switch on, a resolvable binary, the outbound record in a temp
    file, the claude-code breaker closed."""
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '1')
    monkeypatch.delenv('HEVOLVE_LLM_OUTBOUND_BODY', raising=False)
    monkeypatch.setattr(cc, '_resolve_claude_bin', lambda: '/usr/bin/claude')
    log_path = str(tmp_path / 'llm_outbound.jsonl')
    outbound._close_handle()
    monkeypatch.setattr(outbound, '_get_log_path', lambda: log_path)
    llm_provider_breaker.reset(cc.CLAUDE_CODE_PROVIDER_KEY)
    yield log_path
    outbound._close_handle()
    llm_provider_breaker.reset(cc.CLAUDE_CODE_PROVIDER_KEY)


def _records(log_path):
    outbound._close_handle()          # flush the buffered handle
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _run(stdout='ok', stderr='', rc=0):
    class _P:
        pass
    p = _P()
    p.returncode, p.stdout, p.stderr = rc, stdout, stderr
    return p


# ── the record ──────────────────────────────────────────────────────────────

def test_every_invoke_writes_one_outbound_record(_desktop):
    with patch('subprocess.run', return_value=_run('4')):
        r = cc.invoke_claude('2+2?', mode='inference', system='answer tersely')
    assert r['ok']
    recs = _records(_desktop)
    assert len(recs) == 1
    rec = recs[0]
    assert rec['source'] == 'claude-code'
    assert rec['body']['model'] == 'claude-code'
    assert rec['body']['mode'] == 'inference'
    assert rec['body']['messages'] == [
        {'role': 'system', 'content': 'answer tersely'},
        {'role': 'user', 'content': '2+2?'}]
    assert rec['response_status'] == 0
    assert isinstance(rec['latency_ms'], (int, float))


def test_a_failure_to_run_is_recorded_by_its_category(_desktop):
    with patch('subprocess.run', side_effect=FileNotFoundError('gone')):
        r = cc.invoke_claude('q', mode='inference')
    assert r['ok'] is False and r['category'] == 'notfound'
    rec = _records(_desktop)[-1]
    assert rec['response_status'] == 'notfound'


def test_the_switch_refusal_is_recorded_and_spawns_nothing(_desktop, monkeypatch):
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '0')
    with patch('subprocess.run', return_value=_run()) as sr:
        r = cc.invoke_claude('q', mode='inference')
    sr.assert_not_called()
    assert r['category'] == 'off'
    assert _records(_desktop)[-1]['response_status'] == 'off'


# ── the scrub ───────────────────────────────────────────────────────────────

def test_pii_is_scrubbed_on_the_wire_and_raw_in_the_local_record(_desktop):
    prompt = 'mail the owner at sathi@example.com, card 4111 1111 1111 1111'
    system = 'you serve user 555-123-4567'
    with patch('subprocess.run', return_value=_run()) as sr:
        cc.invoke_claude(prompt, mode='inference', system=system)
    cmd = sr.call_args[0][0]
    sent_prompt = cmd[cmd.index('-p') + 1]
    sent_system = cmd[cmd.index('--system-prompt') + 1]
    assert 'sathi@example.com' not in sent_prompt and '4111' not in sent_prompt
    assert '[EMAIL_REDACTED]' in sent_prompt and '[CC_REDACTED]' in sent_prompt
    assert '555-123-4567' not in sent_system and '[PHONE_REDACTED]' in sent_system
    # the local record is the raw truth, like every llama record beside it
    rec = _records(_desktop)[-1]
    assert rec['body']['messages'][0]['content'] == system
    assert rec['body']['messages'][1]['content'] == prompt
    assert rec['body']['egress'] == 'dlp-scrubbed'


def test_a_refusal_records_no_egress(_desktop, monkeypatch):
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '0')
    with patch('subprocess.run', return_value=_run()):
        cc.invoke_claude('q', mode='inference')
    assert _records(_desktop)[-1]['body']['egress'] == 'none'


def test_agentic_runs_are_scrubbed_too(_desktop):
    with patch('subprocess.run', return_value=_run()) as sr:
        cc.invoke_claude('ping 203.0.113.9 and mail a@b.io', mode='agentic', cwd='/repo')
    sent = sr.call_args[0][0][-1]
    assert 'a@b.io' not in sent and '203.0.113.9' not in sent


def test_no_redactor_means_no_egress(_desktop, caplog):
    with patch.dict(sys.modules, {'security.dlp_engine': None}), \
            patch('subprocess.run', return_value=_run()) as sr:
        r = cc.invoke_claude('q', mode='inference')
    sr.assert_not_called()
    assert r['ok'] is False and r['category'] == 'other'
    assert 'redact' in r['error'].lower()
    assert _records(_desktop)[-1]['response_status'] == 'no-redactor'
    assert any('redact' in m.lower() and rec.levelname == 'WARNING'
               for rec, m in ((rec, rec.getMessage()) for rec in caplog.records))


# ── the breaker ─────────────────────────────────────────────────────────────

def _client():
    from flask import Flask
    from integrations.providers.claude_code_endpoint import claude_code_bp
    app = Flask(__name__)
    app.register_blueprint(claude_code_bp)
    return app.test_client()


def test_auth_failures_trip_the_claude_breaker_and_the_shim_refuses_fast(_desktop):
    login = _run('', 'Error: not logged in. Please run /login', rc=1)
    with patch('subprocess.run', return_value=login):
        for _ in range(llm_provider_breaker._threshold):
            r = cc.invoke_claude('q', mode='inference')
            assert cc.classify_failure(r) == 'auth'
    assert llm_provider_breaker.state(cc.CLAUDE_CODE_PROVIDER_KEY) is CircuitState.OPEN
    with patch('subprocess.run', return_value=_run()) as sr:
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]})
    sr.assert_not_called()
    assert resp.status_code == 503
    assert resp.get_json()['error']['category'] == 'auth'
    # the gate read state() non-consumingly: the breaker is still OPEN, not
    # half-open-with-a-probe-claimed
    assert llm_provider_breaker.state(cc.CLAUDE_CODE_PROVIDER_KEY) is CircuitState.OPEN


def test_a_success_closes_the_claude_breaker(_desktop):
    for _ in range(llm_provider_breaker._threshold - 1):
        llm_provider_breaker.record_failure(cc.CLAUDE_CODE_PROVIDER_KEY)
    with patch('subprocess.run', return_value=_run('fine')):
        cc.invoke_claude('q', mode='inference')
    assert llm_provider_breaker.state(cc.CLAUDE_CODE_PROVIDER_KEY) is CircuitState.CLOSED


def test_a_non_auth_failure_does_not_count_against_the_breaker(_desktop):
    with patch('subprocess.run', return_value=_run('', '529 overloaded', rc=1)):
        for _ in range(llm_provider_breaker._threshold + 1):
            cc.invoke_claude('q', mode='inference')
    assert llm_provider_breaker.state(cc.CLAUDE_CODE_PROVIDER_KEY) is CircuitState.CLOSED


# ── the shim joins the caller's record ──────────────────────────────────────

def test_the_shim_binds_the_callers_request_id_to_the_record(_desktop):
    with patch('subprocess.run', return_value=_run('the answer')):
        resp = _client().post('/api/claude/v1/chat/completions',
                              json={'messages': [{'role': 'user', 'content': 'q'}]},
                              headers={'X-HARTOS-Request-ID': 'daemon_goal_abc'})
    assert resp.status_code == 200
    rec = _records(_desktop)[-1]
    assert rec['request_id'] == 'daemon_goal_abc'
    assert rec['source'] == 'claude-code'
