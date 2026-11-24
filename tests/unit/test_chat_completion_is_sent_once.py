"""A chat turn's completion that timed out is not sent again identically.

_pooled_post_with_refusal_check is the ONE way CustomGPT._call (and
ChatQwen3VL._call) posts a turn's completion, with LLM_COMPLETION_TIMEOUT
(3, 120).  It used pooled_post's default, which re-sends a main-llama call
once after ANY failure inside the scheduler slot, and _call's except branches
then re-post the turn to GPT_API: measured on the real _call against a slow
local main server, 4 POSTs and 4x the read budget per turn on three of its
four paths (about 480 s on 120 s).

Now the wrapper asks pooled_post not to re-send after a TIMEOUT
(resend_after_timeout=False).  Two things stay, on purpose:
  - a connection reset is still sent again (the review of d930e46f measured
    that re-send answering where a single send failed);
  - _call's fallback re-post stays: in this desktop's 2026-11-12 server.log it
    answered 3 of 12 timed-out turns (71 s, 100 s, 199 s).
So a timed-out turn waits 2x its budget, not 4x.

Real code throughout: the wrapper's source is run (taken by AST; importing
hart_intelligence_entry is heavy) with the real core.http_pool.pooled_post,
and _call is the real one; the servers are real local ones.
"""
import ast
import http.server
import json as _json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

ENTRY = REPO / 'hart_intelligence_entry.py'
ANSWER = 'from the model'


def _server(mode):
    """A local server.  mode: 'slow' (never answers in time), 'answer', or
    'reset_then_answer' (drops the first connection, then answers)."""
    posts = []
    release = threading.Event()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(time.monotonic())
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if mode == 'slow':
                release.wait(15)
                return
            if mode == 'reset_then_answer' and len(posts) == 1:
                self.close_connection = True   # no response: the client sees a reset
                return
            body = _json.dumps({'choices': [{'message': {'content': ANSWER}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _H)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_address[1]}/v1/chat/completions'

    def stop():
        release.set()
        server.shutdown()
    return url, posts, stop


@pytest.fixture
def main_server(monkeypatch):
    """Start a server and make its port the MAIN llama-server's, so calls go
    through the scheduler, the path that re-sent."""
    import core.http_pool as hp
    stops = []

    def start(mode):
        url, posts, stop = _server(mode)
        stops.append(stop)
        monkeypatch.setattr(hp, '_main_llama_port_cache', url.split(':')[2].split('/')[0])
        return url, posts
    yield start
    for stop in stops:
        stop()


def _wrapper(url):
    """The real _pooled_post_with_refusal_check, defined in a namespace that
    holds only the globals it reads (the read budget shrunk to 1 s)."""
    import core.http_pool as hp
    src = ENTRY.read_text(encoding='utf-8', errors='replace')
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef)
                and n.name == '_pooled_post_with_refusal_check')
    ns = {'os': os, 'logging': logging, 'pooled_post': hp.pooled_post,
          'LLM_COMPLETION_TIMEOUT': (1, 1), 'LLM_AUTH_HEADERS': {},
          'GPT_API': url, 'DRAFT_GPT_API': '', 'app': None}
    exec(compile(ast.Module([node], []), str(ENTRY), 'exec'), ns)  # noqa: S102 - our own source
    return ns['_pooled_post_with_refusal_check']


_BODY = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}


# ── the wrapper, with the real pooled_post ───────────────────────────────

def test_a_timed_out_completion_is_sent_once(main_server):
    url, posts = main_server('slow')
    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        _wrapper(url)(url, json=dict(_BODY))
    waited = time.monotonic() - t0
    assert len(posts) == 1, f'{len(posts)} POSTs after a timeout'
    # The POST count is the guard; the wait only bounds it coarsely (this
    # box is loaded, and the first call also pays the scheduler's import).
    assert waited < 3.0, f'waited {waited:.2f}s on a 1 s read budget'


def test_a_connection_reset_is_still_sent_again_and_answers(main_server):
    """The re-send that saves a turn after a reset stays (review of d930e46f:
    resend=False removed it, and the turn failed)."""
    url, posts = main_server('reset_then_answer')
    resp = _wrapper(url)(url, json=dict(_BODY))
    assert len(posts) == 2
    assert resp.json()['choices'][0]['message']['content'] == ANSWER


def test_a_caller_can_still_ask_for_the_timeout_resend(main_server):
    url, posts = main_server('slow')
    with pytest.raises(requests.exceptions.Timeout):
        _wrapper(url)(url, json=dict(_BODY), resend_after_timeout=True)
    assert len(posts) == 2


# ── pooled_post's options, directly ──────────────────────────────────────

def test_resend_false_still_means_one_send_whatever_the_error(main_server):
    """The older, wider option keeps its contract (core.llm_json's once=)."""
    import core.http_pool as hp
    url, posts = main_server('reset_then_answer')
    with pytest.raises(requests.exceptions.ConnectionError):
        hp.pooled_post(url, json=dict(_BODY), timeout=(1, 1), resend=False)
    assert len(posts) == 1


# ── CustomGPT._call, the real one ────────────────────────────────────────

@pytest.fixture(scope='module')
def hie():
    os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
    return pytest.importorskip('hart_intelligence_entry')


def _model(hie, monkeypatch, *, casual, count, main_url, draft_url=''):
    monkeypatch.setattr(hie, '_g12_kick_off_student', lambda p: None)
    monkeypatch.setattr(hie, '_g12_finalize', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'publish_chat_stage', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'GPT_API', main_url)
    monkeypatch.setattr(hie, 'DRAFT_GPT_API', draft_url)
    monkeypatch.setattr(hie, 'LLM_COMPLETION_TIMEOUT', (1, 1))
    monkeypatch.setenv('HEVOLVE_LANGCHAIN_REFUSAL_OVERRIDE', '0')
    hie.thread_local_data.set_global_intent(None)
    hie.thread_local_data.set_req_token_count(0)
    hie.thread_local_data.set_res_token_count(0)   # read when a turn returns
    try:
        m = hie.CustomGPT(casual_conv=casual)
    except TypeError:
        m = hie.CustomGPT()
        m.casual_conv = casual
    m.count, m.previous_intent, m.call_gpt4 = count, None, 0
    return m


@pytest.mark.parametrize('count, casual', [(0, True), (1, False), (1, True)])
def test_a_timed_out_turn_gets_its_fallback_and_no_more(
        hie, monkeypatch, main_server, count, casual):
    """Main: 4 POSTs and 4x the budget on these paths.  Now 2: the send and
    _call's own fallback re-post, which stays."""
    url, posts = main_server('slow')
    model = _model(hie, monkeypatch, casual=casual, count=count, main_url=url)
    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        model._call('plain prompt')
    waited = time.monotonic() - t0
    assert len(posts) == 2, f'{len(posts)} POSTs for one timed-out turn'
    assert waited < 3.5, f'waited {waited:.2f}s on a 1 s read budget'


def test_the_first_call_survives_a_connection_reset(hie, monkeypatch, main_server):
    """The first non-casual call swallows its own error (it has no fallback),
    so it is the pooled_post re-send that saves it after a reset."""
    url, posts = main_server('reset_then_answer')
    model = _model(hie, monkeypatch, casual=False, count=0, main_url=url)
    out = model._call('plain prompt')
    assert len(posts) == 2
    assert out is not None and ANSWER in out


def test_a_draft_that_times_out_still_falls_back_to_the_main_server(
        hie, monkeypatch, main_server):
    main, main_posts = main_server('answer')
    draft, draft_posts, stop_draft = _server('slow')
    try:
        model = _model(hie, monkeypatch, casual=True, count=1,
                       main_url=main, draft_url=draft)
        out = model._call('plain prompt')
    finally:
        stop_draft()
    assert len(draft_posts) == 1 and len(main_posts) == 1
    assert ANSWER in out
