"""The Claude day cap holds for a turn that already holds the expert (#192).

Live on central 2026-10-25: ModelRegistry._offered logged "configured expert
not offered: claude day budget spent: $3.62 of $3.30", and request
daemon_fbcaabe0-2910-4e3c-a7ef-06d244992ebb went on calling api.anthropic.com
for minutes; the day closed at $4.39 against the $3.30 cap.  The cap was read
only where the expert is OFFERED, so a turn that had it before the ledger
crossed kept spending.

Every call that holds the configured expert now asks the same gate
(budget_gate.expert_budget_allows) before it leaves; the node's own model is
never refused, even a Claude model.  These tests drive the
real wire hook on the real httpx, the real openai SDK and the real autogen
OpenAIWrapper against two live HTTP endpoints.  Only the ledger is modelled:
the meter adds each call's dollar cost (priced by the real
blended_usd_per_1k) and the ledger read sums it, the way MeteredAPIUsage
rows and usd_spent_since do.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

CLAUDE = 'claude-sonnet-4-5'
CAP = 3.30


class _Endpoints(BaseHTTPRequestHandler):
    """/expert answers as Claude, /own as the node's own model; both count."""

    hits = None   # set per server

    def do_POST(self):                                   # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        who = 'expert' if self.path.startswith('/expert') else 'own'
        self.hits[who] = self.hits.get(who, 0) + 1
        body = {
            'id': 'chatcmpl-%s' % who, 'object': 'chat.completion',
            'created': 1, 'model': CLAUDE if who == 'expert' else 'own-model',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant',
                                     'content': 'answered by %s' % who}}],
            # 1000 in + 1000 out of Claude is $0.018 at list price.
            'usage': {'prompt_tokens': 1000, 'completion_tokens': 1000,
                      'total_tokens': 2000},
        }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _serve():
    handler = type('_Counting', (_Endpoints,), {'hits': {}})
    # One thread per connection: a connection that never sends a request line
    # (measured: the single-threaded server's teardown waited on one for hours)
    # must not hold up the test's own calls or the shutdown.
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler.hits


def _pristine(send):
    """httpx's own send, beneath any hook an earlier import installed in this
    process (hart_intelligence_entry installs it at import), so the hook under
    test wraps the transport once, as in production."""
    while getattr(send, '__name__', '') in ('_patched_send', '_patched'):
        cells = dict(zip(send.__code__.co_freevars, send.__closure__ or ()))
        send = (cells.get('_orig_send') or cells['_orig']).cell_contents
    return send


@pytest.fixture
def central(monkeypatch):
    """Central's shape: its own model is a configured API, the expert is
    Claude at another path, the wire hook installed, and a modelled ledger."""
    srv, hits = _serve()
    base = 'http://127.0.0.1:%d' % srv.server_address[1]
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', base + '/own/v1')
    monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'own-model')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', base + '/expert/v1')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', CLAUDE)
    monkeypatch.setenv('HEVOLVE_CLAUDE_BUDGET_USD_DAY', str(CAP))

    import httpx
    monkeypatch.setattr(httpx.Client, 'send', _pristine(httpx.Client.send))
    monkeypatch.setattr(httpx.AsyncClient, 'send', _pristine(httpx.AsyncClient.send))
    import core.llm_outbound_logger as hook
    hook._install_sync_patch(httpx)
    hook._install_async_patch(httpx)
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    monkeypatch.setattr(hook, '_target_ports_cache',
                        ({hook._TARGET_PORT}, time.time() + 3600))

    from integrations.agent_engine import budget_gate as bg
    state = {'start': 0.0, 'ledger': [], 'reads': 0}

    def _meter(model, tokens_in, tokens_out, task_source='own', **_kw):
        rate = bg.blended_usd_per_1k(model, tokens_in, tokens_out)
        if rate is not None:
            state['ledger'].append((tokens_in + tokens_out) / 1000.0 * rate)

    def _spent_since(since, family):
        if family == bg.CAPPED_FAMILY:
            state['reads'] += 1
            return state['start'] + sum(state['ledger'])
        return 0.0

    monkeypatch.setattr(bg, 'meter_llm_call', _meter)
    monkeypatch.setattr(bg, 'usd_spent_since', _spent_since)
    try:
        yield {'base': base, 'hits': hits, 'state': state, 'hook': hook, 'bg': bg}
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_turn_holding_the_expert_stops_calling_it_once_the_ledger_crosses(central):
    """The live failure, one turn: the expert answers while the day is under
    the cap; the call that takes the ledger over is the last one Claude gets,
    and the turn's next call is answered by the node's own model through the
    terminal entry with_local_fallback gives every expert override."""
    import autogen
    from core import autogen_config
    hook, state, hits = central['hook'], central['state'], central['hits']
    state['start'] = 3.29                       # $0.01 under the cap

    expert = autogen_config.resolve_expert_backend()
    turn = autogen.OpenAIWrapper(
        config_list=autogen_config.with_local_fallback([expert]), cache_seed=None)
    msgs = [{'role': 'user', 'content': 'hi'}]
    with hook.lane_context(hook.LANE_REVENUE):
        first = turn.create(messages=msgs).choices[0].message.content
        second = turn.create(messages=msgs).choices[0].message.content

    assert first == 'answered by expert'
    assert second == 'answered by own', (
        'the ledger crossed the cap on the first call (%.3f of %.2f) and the '
        'same turn still called Claude: the cap is read only where the expert '
        'is offered' % (state['start'] + sum(state['ledger']), CAP))
    assert hits.get('expert') == 1, hits
    # Over by one call at most, never more.
    assert len(state['ledger']) == 1


def test_a_refused_call_is_never_retried_and_never_leaves_the_box(central):
    """The refusal tells the openai SDK not to retry (it would re-ask the gate
    twice and sleep between), and the provider never sees the request."""
    import openai
    state, hits = central['state'], central['hits']
    state['start'] = 4.39                       # central's close on 10-25
    client = openai.OpenAI(base_url=central['base'] + '/expert/v1',
                           api_key='dummy', max_retries=2)
    with central['hook'].lane_context(central['hook'].LANE_REVENUE):
        with pytest.raises(openai.RateLimitError) as refused:
            client.chat.completions.create(
                model=CLAUDE, messages=[{'role': 'user', 'content': 'hi'}])
    assert 'day budget' in str(refused.value), refused.value
    assert hits.get('expert', 0) == 0, hits
    assert state['reads'] == 1, 'the SDK retried a refusal %d times' % (state['reads'] - 1)
    assert state['ledger'] == []                # nothing spent, nothing metered


def test_the_async_path_refuses_the_same_way(central):
    """AsyncClient is a second send site (langchain's async invokes)."""
    import openai
    state, hits = central['state'], central['hits']
    state['start'] = 4.39

    async def _call():
        client = openai.AsyncOpenAI(base_url=central['base'] + '/expert/v1',
                                    api_key='dummy', max_retries=2)
        try:
            with central['hook'].lane_context(central['hook'].LANE_REVENUE):
                await client.chat.completions.create(
                    model=CLAUDE, messages=[{'role': 'user', 'content': 'hi'}])
        finally:
            await client.close()

    with pytest.raises(openai.RateLimitError):
        asyncio.run(_call())
    assert hits.get('expert', 0) == 0, hits
    assert state['reads'] == 1


def test_the_nodes_own_model_is_never_refused_by_the_claude_cap(central):
    """Only the capped family: past the cap the node's own model still runs,
    which is what the expert's turn falls back to."""
    import openai
    central['state']['start'] = 4.39
    client = openai.OpenAI(base_url=central['base'] + '/own/v1',
                           api_key='dummy', max_retries=0)
    reply = client.chat.completions.create(
        model='own-model', messages=[{'role': 'user', 'content': 'hi'}])
    assert reply.choices[0].message.content == 'answered by own'
    assert central['hits'].get('own') == 1


def test_the_nodes_own_claude_model_is_never_refused_either(central):
    """The cap governs the configured expert, not the model family: a node
    whose own model is a Claude model keeps its own turns past the cap, since
    refusing the node's own model would block the turn instead of withholding
    anything (budget_gate: withholding is never blocking)."""
    import openai
    central['state']['start'] = 4.39
    client = openai.OpenAI(base_url=central['base'] + '/own/v1',
                           api_key='dummy', max_retries=0)
    reply = client.chat.completions.create(
        model='claude-haiku-4-5', messages=[{'role': 'user', 'content': 'hi'}])
    assert reply.choices[0].message.content == 'answered by own'
    assert central['hits'].get('own') == 1


def test_a_call_to_an_endpoint_the_node_did_not_configure_is_left_alone(central):
    """Scope guard, not a fail-first test: the cap covers the dollars HARTOS
    meters (its configured endpoints).  A call to any other host is left as
    it was (owner rule 2026-10-06; _is_configured_endpoint), even a Claude
    call past the cap."""
    import openai
    central['state']['start'] = 4.39
    other, other_hits = _serve()
    try:
        client = openai.OpenAI(
            base_url='http://127.0.0.1:%d/expert/v1' % other.server_address[1],
            api_key='dummy', max_retries=0)
        reply = client.chat.completions.create(
            model=CLAUDE, messages=[{'role': 'user', 'content': 'hi'}])
        assert reply.choices[0].message.content == 'answered by expert'
        assert other_hits.get('expert') == 1
    finally:
        other.shutdown()
        other.server_close()
