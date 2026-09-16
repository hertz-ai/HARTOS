"""Guard: a dispatcher-selected tier must have somewhere to land.

Live root cause, measured 2026-09-06 on the running app (dev Flask, agents
74769894436 and 25214546249).  Every reuse turn that the dispatcher routed to
the claude-code EXPERT tier died, and the death is fully attributed:

  1. speculative_dispatcher._build_dispatch_payload (:1721) and dispatch.py
     (:904) put ``'model_config': model.to_config_list()`` in the inner /chat
     payload.  ``ModelBackend.to_config_list`` (model_registry.py:64) returns
     ``[self.config_list_entry]`` — exactly ONE entry.
  2. hart_intelligence_entry chat() (:9197, :9248) stores it verbatim:
     ``thread_local_data.set_model_config_override(model_config)``.
  3. Every autogen agent the reuse pipeline builds reads it —
     reuse_recipe.create_agents_for_role (:728),
     create_agents_for_user (:956), helper.get_llm_config (:2553),
     hart_intelligence_entry (:7290, :7370) — as
     ``override or config_list``.  So the whole GroupChat runs on a
     one-entry config_list pointing at the expert.
  4. The expert 503s.  Two live shapes, same window:
       "claude-code at capacity"   — claude_code_endpoint's own
                                     BoundedSemaphore(HART_CLAUDE_MAX_CONCURRENT,
                                     default 2), i.e. ORDINARY concurrency
       "claude not on PATH"        — category 'notfound'
     Both are deliberately mapped to 503 by _FAIL_STATUS, whose comments state
     the intent verbatim: "a lapsed subscription must not error the OS; it
     degrades to local" and "caller falls back to local".
  5. Nothing falls back.  With one entry autogen's OpenAIWrapper.create has no
     next client (autogen/oai/client.py:687 ``last = len(self._clients) - 1``;
     :783 ``except APIError ... if i == last: raise``), so
     openai.InternalServerError propagates to reuse_recipe.get_agent_response
     (:3842) where a blanket ``except`` swallows it and the turn ends.

Measured in gui_app.log 20:48-21:27: 6x per drive on an agent whose recipe
names no coding action at all — i.e. it is not the recipe's content that
routes here, and a node whose copilot is merely BUSY loses the turn entirely.

The fix uses the ENGINE's own ladder rather than adding a dispatch path:
a second config_list entry IS autogen's fallback, and it fires on exactly this
exception class.  The selected tier stays FIRST, so routing still belongs to
the dispatcher — this only gives it somewhere to land.

These are functional tests, not source assertions.  A-D exercise the real
composition function and assert on what it returns; E drives the real autogen
OpenAIWrapper against two live HTTP endpoints where entry 0 returns the
measured 503 body, and asserts a completion still comes back.  F is E's
non-vacuity twin: the same 503 against a one-entry list must still raise, or
E would be proving nothing.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core import autogen_config  # noqa: E402

_LOCAL = {'model': 'local', 'api_key': 'dummy',
          'base_url': 'http://127.0.0.1:8080/v1', 'price': [0, 0]}
_EXPERT = {'model': 'claude-code', 'api_key': 'dummy',
           'base_url': 'http://localhost:5000/api/claude/v1', 'price': [0, 0]}


def _pin_node_backend(monkeypatch, entries):
    """The node's own backend, without probing ports or reading the vault."""
    monkeypatch.setattr(autogen_config, 'get_autogen_config_list',
                        lambda: [dict(e) for e in entries])


def test_a_selected_expert_tier_gets_a_local_entry_after_it(monkeypatch):
    """The load-bearing assertion — this is the live 2026-09-06 failure."""
    _pin_node_backend(monkeypatch, [_LOCAL])
    out = autogen_config.with_local_fallback([_EXPERT])
    assert len(out) == 2, (
        "the dispatcher's one-entry config_list has nothing to fall to, so a "
        "503 from the expert kills the turn.  claude_code_endpoint's own "
        "_FAIL_STATUS says a 503 means 'caller falls back to local'; autogen's "
        "config_list is that fallback (client.py:783 'if i == last: raise').  "
        "got %r" % (out,))
    assert out[1]['base_url'] == _LOCAL['base_url'], (
        'the terminal entry must be this node\'s own backend')


def test_b_the_selected_tier_is_still_tried_first(monkeypatch):
    """Non-vacuity: the fix must not quietly delete the expert tier.

    Putting local first would also stop the crash — and would route every
    escalated turn to the local model while the dispatcher believed it had
    reached the expert.  That is the #69-class defect this repo already paid
    for once (233 outbound calls carrying model='local' on EXPERT turns).
    """
    _pin_node_backend(monkeypatch, [_LOCAL])
    out = autogen_config.with_local_fallback([_EXPERT])
    assert out[0]['model'] == 'claude-code', (
        'the dispatcher-selected tier must remain entry 0 — autogen tries '
        'clients in order, so anything before it takes the turn instead.  '
        'got %r' % (out,))


def test_c_a_local_routed_turn_does_not_get_a_duplicate(monkeypatch):
    """Same endpoint twice is a wasted retry against a model that just failed."""
    _pin_node_backend(monkeypatch, [_LOCAL])
    out = autogen_config.with_local_fallback([dict(_LOCAL)])
    assert len(out) == 1, (
        'an override that already IS the node backend needs no second copy of '
        'itself; retrying the same endpoint is not a fallback.  got %r' % (out,))


def test_d_no_override_is_left_alone(monkeypatch):
    """Empty/None must stay falsy so `override or config_list` still works.

    Every consumer spells the default as ``override or config_list``.  If this
    helper turned a missing override into a non-empty list it would BECOME the
    override at all five call sites and quietly bypass that default.
    """
    _pin_node_backend(monkeypatch, [_LOCAL])
    assert not autogen_config.with_local_fallback(None)
    assert not autogen_config.with_local_fallback([])


class _Endpoints(BaseHTTPRequestHandler):
    """Two OpenAI-compatible paths: /expert 503s, /local answers."""

    def do_POST(self):                                   # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path.startswith('/expert'):
            # The exact live shape, verbatim from claude_code_endpoint.
            body = {'error': {'message': 'claude-code at capacity',
                              'type': 'overloaded_error'}}
            self.send_response(503)
        else:
            body = {
                'id': 'chatcmpl-local-1', 'object': 'chat.completion',
                'created': 1, 'model': 'local',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant',
                                         'content': 'answered by local'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                          'total_tokens': 2},
            }
            self.send_response(200)
        raw = json.dumps(body).encode()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                           # keep pytest output clean
        pass


def test_e_a_503_from_the_selected_tier_is_answered_by_local(monkeypatch):
    """Functional end-to-end: the composed list actually survives a live 503.

    This is the whole point of the change, exercised against the real
    autogen OpenAIWrapper and real HTTP — not a description of it.  Entry 0 is
    an endpoint that returns the measured 503 body; entry 1 is this node's
    backend.  A passing run means a reuse GroupChat whose dispatcher picked a
    failing tier still gets a completion instead of the
    openai.InternalServerError that ended every turn on 2026-09-06.
    """
    import autogen

    srv = HTTPServer(('127.0.0.1', 0), _Endpoints)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        expert = {'model': 'claude-code', 'api_key': 'dummy',
                  'base_url': 'http://127.0.0.1:%d/expert/v1' % port,
                  'price': [0, 0], 'max_retries': 0}
        local = {'model': 'local', 'api_key': 'dummy',
                 'base_url': 'http://127.0.0.1:%d/local/v1' % port,
                 'price': [0, 0], 'max_retries': 0}
        _pin_node_backend(monkeypatch, [local])

        composed = autogen_config.with_local_fallback([expert])
        wrapper = autogen.OpenAIWrapper(config_list=composed, cache_seed=None)
        resp = wrapper.create(messages=[{'role': 'user', 'content': 'hi'}])

        text = resp.choices[0].message.content
        assert text == 'answered by local', (
            "the selected tier 503'd and the turn produced no completion — "
            "which is the live failure: openai.InternalServerError propagates "
            "into reuse_recipe.get_agent_response and the turn ends.  got %r"
            % (text,))
    finally:
        srv.shutdown()
        srv.server_close()


def test_f_without_the_fallback_the_same_503_kills_the_call(monkeypatch):
    """Non-vacuity for test E: prove the one-entry list really does fail.

    Same server, same 503, same wrapper — only the fallback entry removed.  If
    this did NOT raise, test E would be passing for some unrelated reason and
    would be worthless as evidence.
    """
    import autogen
    import openai

    srv = HTTPServer(('127.0.0.1', 0), _Endpoints)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        expert = {'model': 'claude-code', 'api_key': 'dummy',
                  'base_url': 'http://127.0.0.1:%d/expert/v1' % port,
                  'price': [0, 0], 'max_retries': 0}
        wrapper = autogen.OpenAIWrapper(config_list=[expert], cache_seed=None)
        raised = None
        try:
            wrapper.create(messages=[{'role': 'user', 'content': 'hi'}])
        except openai.APIStatusError as exc:
            raised = exc
        assert raised is not None and raised.status_code == 503, (
            'a one-entry config_list was expected to propagate the 503 — if it '
            'does not, the fall-through test above proves nothing')
    finally:
        srv.shutdown()
        srv.server_close()
