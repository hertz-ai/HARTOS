"""A goal turn that fell back to the expert stays under the Claude day cap.

Owner, 2026-11-16, choosing "Fall back to Haiku" for goal turns while the free
model's day quota is spent: "the fallback per day will also be under the 3.3
budget we have?"  It is, because the fallback calls Claude as the CONFIGURED
EXPERT (HEVOLVE_EXPERT_LLM_*, the expert's own model_config entry that
dispatch.revenue_expert_config hands the turn), never by pointing the node's
own model (HEVOLVE_LLM_*) at Claude: the wire's per-call gate
(core.llm_outbound_logger._day_cap_refusal, 9caee5961) asks
budget_gate.expert_budget_allows before every call that holds the configured
expert, and never refuses the node's own model.

The whole path, on the real wire: the real AgentDaemon._tick with speculation
on (central's setting) steps aside from the refusing free model; the real
dispatch_goal gives the turn the configured expert; this node's /chat is a
tiny Flask app doing what /chat does with a model_config override (autogen's
real OpenAIWrapper over with_local_fallback, a failed call answered with
user_facing_error); the real openai SDK and real httpx go through the real
wire hook to two live HTTP endpoints on two loopback hosts (the breaker is
keyed by host, and on central the free model and the expert are different
hosts).  Only the ledger is modelled, as in test_claude_day_cap_holds_mid_
turn: the meter adds each call's dollars (the real blended_usd_per_1k) and
the ledger read sums them.

    python -m pytest tests/unit/test_a_fallback_expert_turn_stops_at_the_day_cap.py -q
"""
import json
import socket
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from core.agent_tools import user_facing_error
from integrations.agent_engine import agent_daemon
from integrations.agent_engine.model_registry import (
    CONFIGURED_EXPERT_ID, _own_llm_target, ensure_configured_expert_registered,
    model_registry)
from tests.unit.test_a_spent_free_model_hands_goal_turns_to_the_expert import (
    CAP, GOAL_ID, _FastChat, _models, _speculator, _tick, breaker)  # noqa: F401
from tests.unit.test_claude_day_cap_holds_mid_turn import _pristine
from tests.unit.test_speculative_turn_is_judged_by_its_reply import Goal

EXPERT_MODEL = 'claude-haiku-5-5'      # central's expert
OWN_MODEL = 'free/model'


class _Endpoints(BaseHTTPRequestHandler):
    """/expert answers as Claude with a usage block; /own is the free model
    whose day quota is spent: openrouter's 429, as measured on central."""

    hits = None

    def do_POST(self):                                   # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        who = 'expert' if self.path.startswith('/expert') else 'own'
        self.hits[who] = self.hits.get(who, 0) + 1
        if who == 'expert':
            status, extra = 200, {}
            body = {'id': 'chatcmpl-x', 'object': 'chat.completion',
                    'created': 1, 'model': EXPERT_MODEL,
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant',
                                             'content': 'drafted the post'}}],
                    'usage': {'prompt_tokens': 1000, 'completion_tokens': 1000,
                              'total_tokens': 2000}}
        else:
            reset_ms = int((time.time() + 6 * 3600) * 1000)
            status, extra = 429, {'retry-after-ms': '1'}
            body = {'error': {
                'message': 'Rate limit exceeded: free-models-per-day',
                'code': 429,
                'metadata': {'headers': {'X-RateLimit-Limit': '50',
                                         'X-RateLimit-Remaining': '0',
                                         'X-RateLimit-Reset': str(reset_ms)}}}}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _serve(host, hits):
    handler = type('_Counting', (_Endpoints,), {'hits': hits})
    srv = ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _second_loopback_host():
    """A loopback address other than 127.0.0.1 (127.0.0.2 on Windows and
    Linux); else 'localhost', which resolves to the 127.0.0.1 server under
    another breaker key."""
    try:
        with socket.socket() as s:
            s.bind(('127.0.0.2', 0))
        return '127.0.0.2', '127.0.0.2'
    except OSError:
        return '127.0.0.1', 'localhost'


@pytest.fixture
def central(monkeypatch):
    """Central's shape: its own model a hosted free model whose day quota is
    spent (breaker OPEN), the expert Claude on another host, the wire hook
    installed, and a modelled ledger."""
    hits = {}
    expert_srv = _serve('127.0.0.1', hits)
    bind, own_host = _second_loopback_host()
    own_srv = _serve(bind, hits)
    own_url = f'http://{own_host}:{own_srv.server_address[1]}/own/v1'
    expert_url = f'http://127.0.0.1:{expert_srv.server_address[1]}/expert/v1'
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', own_url)
    monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', OWN_MODEL)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', expert_url)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', EXPERT_MODEL)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_API_KEY', 'test-only')
    monkeypatch.delenv('HEVOLVE_EXPERT_LANES', raising=False)

    import httpx
    monkeypatch.setattr(httpx.Client, 'send', _pristine(httpx.Client.send))
    monkeypatch.setattr(httpx.AsyncClient, 'send',
                        _pristine(httpx.AsyncClient.send))
    import core.llm_outbound_logger as hook
    hook._install_sync_patch(httpx)
    hook._install_async_patch(httpx)
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    monkeypatch.setattr(hook, '_target_ports_cache',
                        ({hook._TARGET_PORT}, time.time() + 3600))

    from integrations.agent_engine import budget_gate as bg
    one_call = bg.blended_usd_per_1k(EXPERT_MODEL, 1000, 1000) * 2.0
    state = {'start': CAP - one_call / 2.0, 'ledger': []}

    def _meter(model, tokens_in, tokens_out, task_source='own', **_kw):
        rate = bg.blended_usd_per_1k(model, tokens_in, tokens_out)
        if rate is not None:
            state['ledger'].append((tokens_in + tokens_out) / 1000.0 * rate)

    def _spent_since(since, family):
        if family == bg.CAPPED_FAMILY:
            return state['start'] + sum(state['ledger'])
        return 0.0

    monkeypatch.setattr(bg, 'meter_llm_call', _meter)
    monkeypatch.setattr(bg, 'usd_spent_since', _spent_since)
    assert ensure_configured_expert_registered()
    try:
        yield {'own_url': own_url, 'hits': hits, 'state': state, 'hook': hook}
    finally:
        model_registry.unregister(CONFIGURED_EXPERT_ID)
        for srv in (expert_srv, own_srv):
            srv.shutdown()
            srv.server_close()


def _spend_the_free_quota(central):
    """The morning: three calls to the free model answer its spent-quota 429
    through the real hook, which opens the free model's breaker."""
    import openai
    client = openai.OpenAI(base_url=central['own_url'], api_key='x',
                           max_retries=0)
    for _ in range(3):
        with pytest.raises(openai.RateLimitError):
            client.chat.completions.create(
                model=OWN_MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    central['hits'].clear()


def _chat_app(turns):
    """This node's /chat for a goal turn with a model_config override: the
    override first, this node's own model last (with_local_fallback), two
    model calls (two steps), a failed call answered as the pipeline answers
    it (user_facing_error)."""
    from flask import Flask, jsonify, request
    import autogen
    from core.autogen_config import get_autogen_config_list, with_local_fallback
    app = Flask('own_chat_day_cap')

    @app.post('/chat')
    def _chat():
        body = request.get_json() or {}
        override = body.get('model_config')
        turns.append(_models(override))
        msgs = [{'role': 'user', 'content': body.get('prompt') or 'go'}]
        try:
            client = autogen.OpenAIWrapper(
                config_list=(with_local_fallback(override) if override
                             else get_autogen_config_list()),
                cache_seed=None)
            client.create(messages=msgs)
            reply = client.create(messages=msgs).choices[0].message.content
        except Exception as e:
            reply = user_facing_error(e)
        return jsonify({'response': reply})

    mod = types.ModuleType('hart_intelligence_entry')
    mod.app = app
    return mod


def _refusals(warn):
    return sum(1 for c in warn.call_args_list
               if c.args and str(c.args[0]).startswith('[outbound] %s call refused'))


def test_the_fallback_turn_stops_calling_the_expert_at_the_cap_and_waits(central):
    """FAILS BEFORE THE FIX: the speculative branch ran a fast turn on the
    refusing free model and no turn ever reached the expert."""
    hook, hits, state = central['hook'], central['hits'], central['state']
    _spend_the_free_quota(central)
    goal = Goal(gid=GOAL_ID)
    d, fast, turns = _speculator(central['own_url'], OWN_MODEL), _FastChat(''), []
    app = _chat_app(turns)

    with patch.object(hook.logger, 'warning', wraps=hook.logger.warning) as warn:
        # The fallback turn starts under the cap; its first call takes the
        # ledger over it.
        _tick(goal, d, fast, app, None, own_target=_own_llm_target)
        assert fast.turns == [], 'a fast turn started on the refusing free model'
        assert turns == [[EXPERT_MODEL]], \
            'the turn must hold the CONFIGURED EXPERT entry, never the own model'
        assert hits.get('expert') == 1, hits
        assert len(state['ledger']) == 1, 'over the cap by more than one call'
        assert state['start'] + sum(state['ledger']) > CAP
        assert _refusals(warn) == 1, \
            'the next expert call must be refused once, with no retry'
        assert agent_daemon._dispatch_backoff.get(GOAL_ID) is None, \
            'the turn that crossed the cap took a strike'
        own_calls = hits.get('own', 0)
        assert own_calls >= 1, 'the refused call did not fall to the own model'

        # The ticks after: no model can take the goal, so it waits.
        for _ in range(2):
            _tick(goal, d, fast, app, None, own_target=_own_llm_target)
        assert turns == [[EXPERT_MODEL]], 'a turn started past the cap'
        assert hits.get('expert') == 1 and hits.get('own', 0) == own_calls, hits
        assert _refusals(warn) == 1
        assert fast.turns == [] and d._expert_pool.jobs == []
    assert agent_daemon._dispatch_backoff.get(GOAL_ID) is None, \
        'the deferral past the cap carried a strike'
    assert goal.status == 'active'
