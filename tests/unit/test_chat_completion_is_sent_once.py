"""The chat turn's completion is sent once: its wait is the budget it names.

_pooled_post_with_refusal_check is the ONE way CustomGPT._call posts a turn's
completion to the main llama-server, with LLM_COMPLETION_TIMEOUT (3, 120).
It used pooled_post's default, which re-sends once after a read timeout
inside the scheduler slot, so a person's turn could wait about 240 s on a
120 s budget.  Measured on the draft call in the review of HARTOS #165 (2
POSTs and 2.06x its timeout on the main port); this is the same mechanism
on the main chat call.

The function's real source is run (taken from the module by AST: importing
hart_intelligence_entry is heavy) with the real core.http_pool.pooled_post,
against a real local server that answers slower than the budget.
"""
import ast
import http.server
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


def _real_function(**globals_):
    """The real _pooled_post_with_refusal_check, defined in a namespace that
    holds only the globals it reads."""
    src = ENTRY.read_text(encoding='utf-8', errors='replace')
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef)
                and n.name == '_pooled_post_with_refusal_check')
    ns = {'os': os, 'logging': logging, **globals_}
    exec(compile(ast.Module([node], []), str(ENTRY), 'exec'), ns)  # noqa: S102 - our own source
    return ns['_pooled_post_with_refusal_check']


@pytest.fixture
def slow_main_server(monkeypatch):
    """A main llama-server slower than any budget here, counting POSTs."""
    import core.http_pool as hp
    posts = []
    release = threading.Event()

    class _Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(time.monotonic())
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            release.wait(15)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Slow)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    # This port is the MAIN llama-server's: the call goes through the
    # scheduler, the path that re-sent.
    monkeypatch.setattr(hp, '_main_llama_port_cache', str(port))
    try:
        yield f'http://127.0.0.1:{port}/v1/chat/completions', posts
    finally:
        release.set()
        server.shutdown()


def test_a_slow_turn_is_posted_once_and_waits_its_budget(slow_main_server):
    import core.http_pool as hp
    url, posts = slow_main_server
    post = _real_function(
        pooled_post=hp.pooled_post,
        LLM_COMPLETION_TIMEOUT=(1, 1),     # the real (3, 120), shrunk
        LLM_AUTH_HEADERS={}, GPT_API=url, DRAFT_GPT_API='',
        app=None)
    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        post(url, json={'model': 'm', 'messages': [
            {'role': 'user', 'content': 'hi'}], 'max_tokens': 5})
    waited = time.monotonic() - t0
    assert len(posts) == 1, f'{len(posts)} POSTs for one turn completion'
    assert waited < 1.8, f'waited {waited:.2f}s on a 1 s read budget'


def test_a_caller_can_still_ask_for_the_resend(slow_main_server):
    """resend is setdefault, like timeout: a caller that passes it wins."""
    import core.http_pool as hp
    url, posts = slow_main_server
    post = _real_function(
        pooled_post=hp.pooled_post, LLM_COMPLETION_TIMEOUT=(1, 1),
        LLM_AUTH_HEADERS={}, GPT_API=url, DRAFT_GPT_API='', app=None)
    with pytest.raises(requests.exceptions.Timeout):
        post(url, json={'model': 'm', 'messages': [
            {'role': 'user', 'content': 'hi'}]}, resend=True)
    assert len(posts) == 2


# ── CustomGPT._call: the fallback re-post ─────────────────────────────────
# Its except branches re-post the turn to GPT_API.  After a timeout ON
# GPT_API that was a second full wait (with pooled_post's re-send, 4 POSTs and
# 4x the budget per turn, measured on the real _call).  A draft that fails
# still falls back to GPT_API.

def _slow_or_fast_server(answer):
    """A local server: slow (answer None) or answering a completion."""
    import json as _json
    posts = []
    release = threading.Event()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(time.monotonic())
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if answer is None:
                release.wait(15)
                return
            body = _json.dumps({'choices': [{'message': {'content': answer}}]}).encode()
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


@pytest.fixture(scope='module')
def hie():
    os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
    return pytest.importorskip('hart_intelligence_entry')


def _model(hie, monkeypatch, *, casual, count, main_url, draft_url=''):
    import core.http_pool as hp
    port = main_url.split(':')[2].split('/')[0]
    monkeypatch.setattr(hp, '_main_llama_port_cache', port)
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
def test_a_turn_that_timed_out_on_the_main_server_is_not_posted_again(
        hie, monkeypatch, count, casual):
    url, posts, stop = _slow_or_fast_server(None)
    try:
        model = _model(hie, monkeypatch, casual=casual, count=count, main_url=url)
        t0 = time.monotonic()
        with pytest.raises(requests.exceptions.Timeout):
            model._call('plain prompt')
        waited = time.monotonic() - t0
    finally:
        stop()
    assert len(posts) == 1, f'{len(posts)} POSTs for one turn'
    assert waited < 1.8, f'waited {waited:.2f}s on a 1 s read budget'


def test_a_draft_that_times_out_still_falls_back_to_the_main_server(hie, monkeypatch):
    draft, draft_posts, stop_draft = _slow_or_fast_server(None)
    main, main_posts, stop_main = _slow_or_fast_server('from the main model')
    try:
        model = _model(hie, monkeypatch, casual=True, count=1,
                       main_url=main, draft_url=draft)
        out = model._call('plain prompt')
    finally:
        stop_draft()
        stop_main()
    assert len(draft_posts) == 1 and len(main_posts) == 1
    assert 'from the main model' in out
