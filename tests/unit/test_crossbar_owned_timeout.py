"""Execute the actual small transport helper without booting the whole app."""
import ast
import logging
from pathlib import Path
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def transport(client):
    path = Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_http_crossbar_publish')
    scope = {'client': client, 'logging': logging,
             '_crossbar_client_lock': threading.Lock()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope['_http_crossbar_publish']


@pytest.mark.parametrize('raises', [False, True])
def test_client_timeout_restored_and_socket_defaults_untouched(raises):
    seen = []
    client = SimpleNamespace(timeout=None)
    def publish(topic, payload):
        seen.append((topic, payload, client.timeout))
        if raises:
            raise OSError('offline')
    client.publish = publish
    original = socket.getdefaulttimeout()
    transport(client)('com.hertzai.hevolve.chat.u1', 'original-bytes', 0.25)
    assert seen == [('com.hertzai.hevolve.chat.u1', 'original-bytes', 0.25)]
    assert client.timeout is None
    assert socket.getdefaulttimeout() is original


def test_concurrent_custom_timeouts_do_not_race():
    seen = []
    client = SimpleNamespace(timeout=2.0)
    def publish(topic, payload):
        seen.append((payload, client.timeout))
    client.publish = publish
    send = transport(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, 'topic', value, value) for value in range(1, 21)]
        for future in futures:
            future.result(timeout=5)
    assert sorted(seen) == [(value, value) for value in range(1, 21)]
    assert client.timeout == 2.0


def test_alternate_sdk_and_missing_client_preserve_compatibility():
    alternate = SimpleNamespace(publish=Mock())
    transport(alternate)('topic', 'bytes')
    alternate.publish.assert_called_once_with('topic', 'bytes')
    transport(None)('topic', 'bytes')


@pytest.mark.parametrize('slow', [False, True])
def test_installed_sdk_uses_owned_timeout_over_real_local_http(slow):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from crossbarhttp.crossbarhttp import Client
    received = []
    request_seen = threading.Event()
    release_response = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            request_seen.set()
            if slow:
                release_response.wait(3)
            body = b'{"id":123}'
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the client times out first.
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = Client(f'http://127.0.0.1:{server.server_port}/publish', timeout=2)
    before = socket.getdefaulttimeout()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(transport(client), 'test.recovery', 'exact-bytes', 0.1)
            assert request_seen.wait(2), 'SDK did not reach the local HTTP server'
            future.result(timeout=2)  # Slow server cannot trap the executor.
        assert received == [{'topic':'test.recovery', 'args':['exact-bytes'], 'kwargs':{}}]
        assert client.timeout == 2
        assert socket.getdefaulttimeout() is before
    finally:
        release_response.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_an_outage_is_announced_once_and_its_end_once(caplog):
    """Offline Crossbar is an ordinary state: one warning (no traceback) when
    publishing starts failing, debug while it keeps failing, one line when it
    recovers -- not an ERROR traceback on every publish."""
    state = {'down': True}
    client = SimpleNamespace(timeout=None)
    def publish(topic, payload):
        if state['down']:
            raise ConnectionError('refused')
    client.publish = publish
    send = transport(client)
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            send('topic', 'x')
        state['down'] = False
        send('topic', 'x')
        send('topic', 'x')
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and 'refused' in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info]
    assert sum('still failing' in r.getMessage() for r in caplog.records) == 2
    assert sum('recovered' in r.getMessage() for r in caplog.records) == 1


def test_one_failing_topic_beside_healthy_ones_warns_once(caplog):
    """A single global flag flipped on every alternation: a warning and a
    "recovered" line per publish.  Tracked per topic, the broken topic warns
    once and the healthy ones say nothing."""
    client = SimpleNamespace(timeout=None)
    def publish(topic, payload):
        if topic == 'bad':
            raise ValueError('rejected')
    client.publish = publish
    send = transport(client)
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            send('bad', 'x')
            send('good', 'x')
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    assert not [r for r in caplog.records if 'recovered' in r.getMessage()]


def test_concurrent_failures_warn_once(caplog):
    client = SimpleNamespace(timeout=None)
    def publish(topic, payload):
        raise ConnectionError('refused')
    client.publish = publish
    send = transport(client)
    with caplog.at_level(logging.WARNING), ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(send, 'topic', 'x') for _ in range(20)]:
            f.result(timeout=5)
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
