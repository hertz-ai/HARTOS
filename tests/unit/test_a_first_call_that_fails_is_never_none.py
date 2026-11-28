"""A turn's first model call that fails is never answered with None.

CustomGPT._call's first non-casual call (count 0: the first model call of
every agent-bound turn) had its own try/except that swallowed any error.
Its `checker` then stayed None, neither result branch ran, and _call fell
off its end: it returned None, which langchain turns into a ValidationError
and /chat into a 500.  Every other path of _call lets the error reach its
outer except, which re-posts the turn to GPT_API once; that re-post answered
3 of 12 timed-out turns in this desktop's 2026-11-12 server.log.  Found in
the review of HARTOS #170.

Real _call, real core.http_pool.pooled_post, real local servers.
"""
import http.server
import json as _json
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

ANSWER = 'from the model'


def _server(replies):
    """A local server answering each POST in turn from ``replies``: 'slow'
    (never in time), 'garbage' (200, not JSON) or 'answer'.  The last reply
    repeats."""
    posts = []
    release = threading.Event()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(time.monotonic())
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            kind = replies[min(len(posts), len(replies)) - 1]
            if kind == 'slow':
                release.wait(15)
                return
            body = (b'<html>bad gateway</html>' if kind == 'garbage' else
                    _json.dumps({'choices': [{'message': {'content': ANSWER}}]}).encode())
            self.send_response(200)
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


@pytest.fixture
def first_call(hie, monkeypatch):
    """A CustomGPT at its first, non-casual call, pointed at ``url``."""
    import core.http_pool as hp
    stops = []

    def make(replies):
        url, posts, stop = _server(replies)
        stops.append(stop)
        monkeypatch.setattr(hp, '_main_llama_port_cache', url.split(':')[2].split('/')[0])
        monkeypatch.setattr(hie, '_g12_kick_off_student', lambda p: None)
        monkeypatch.setattr(hie, '_g12_finalize', lambda *a, **k: None)
        monkeypatch.setattr(hie, 'publish_chat_stage', lambda *a, **k: None)
        monkeypatch.setattr(hie, 'GPT_API', url)
        monkeypatch.setattr(hie, 'DRAFT_GPT_API', '')
        monkeypatch.setattr(hie, 'LLM_COMPLETION_TIMEOUT', (1, 1))
        monkeypatch.setenv('HEVOLVE_LANGCHAIN_REFUSAL_OVERRIDE', '0')
        hie.thread_local_data.set_global_intent(None)
        hie.thread_local_data.set_req_token_count(0)
        hie.thread_local_data.set_res_token_count(0)
        try:
            model = hie.CustomGPT(casual_conv=False)
        except TypeError:
            model = hie.CustomGPT()
            model.casual_conv = False
        model.count, model.previous_intent, model.call_gpt4 = 0, None, 0
        return model, posts
    yield make
    for stop in stops:
        stop()


def test_a_first_reply_that_is_not_json_falls_back_and_answers(first_call):
    """A 200 whose body is not JSON (a proxy's error page) raised inside the
    swallowed block: the turn got None.  Now the fallback re-post answers."""
    model, posts = first_call(['garbage', 'answer'])
    out = model._call('plain prompt')
    assert out is not None, '_call returned None'
    assert ANSWER in out
    assert len(posts) == 2


def test_a_first_call_that_times_out_raises_instead_of_returning_none(first_call):
    """When the fallback fails too, the error reaches the caller as the
    error it is, as on _call's other paths, not as None."""
    model, _ = first_call(['slow'])
    with pytest.raises(requests.exceptions.Timeout):
        model._call('plain prompt')


def test_a_first_call_that_answers_is_unchanged(first_call):
    model, posts = first_call(['answer'])
    out = model._call('plain prompt')
    assert ANSWER in out
    assert len(posts) == 1
