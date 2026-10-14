"""A WSGI response with no body still starts: 304, HEAD and 204 answer as
themselves through the transport, never as a 500.

Measured on the HART OS node, generation 21 (2026-10-12): through the shell's
proxy to the Nunba daemon (unix socket, Hypercorn, core.serve.build_asgi_app),
a conditional GET on any asset answered 500 and a HEAD answered 500 while the
plain GET answered 200; Nunba's journal carried one
"'HTTPStream' object has no attribute 'response'" traceback per failure (31 in
the first ten minutes). The browser revalidates assets it already holds, so on
a panel reopen 18 of 33 requests were revalidations and Nunba's page could not
boot inside the assistant panel.

The cause is the WSGI wrapper of the hypercorn the node's Nunba environment
ships, 0.16.0 from nixpkgs (requirements.txt names 0.17.3, whose wrapper
starts before the loop and has no such bug): its run_app sends
http.response.start only from inside the body loop, so an EMPTY body never
starts, and handle_http's unconditional end-of-body send reaches
HTTPStream.app_send with no response recorded. core.serve now gives the
middleware a WSGIWrapper whose run_app starts an empty response after the
iteration.

These drive the REAL middleware as an ASGI app (the idiom of
test_local_upload_has_no_cap.py) with a real Flask app: the one mocked
boundary is the ASGI send, recorded. On a machine whose hypercorn already
starts before the loop they pass either way; on the node (2026-10-12, run
with the node's own python against the shipped and the patched module) the
three empty-body cases failed on the shipped module and passed on this one.
"""
import asyncio

import pytest

LOCAL_PEER = ('127.0.0.1', 50000)


def _asset_app():
    from flask import Flask, Response, request
    app = Flask(__name__)

    @app.route('/asset')
    def asset():
        # The shape Flask's static serving produces: an ETag, and 304 with an
        # empty body when the client already holds the bytes.
        etag = '"v1"'
        if request.headers.get('If-None-Match') == etag:
            return Response(status=304, headers={'ETag': etag})
        return Response(b'bytes', status=200, headers={'ETag': etag})

    @app.route('/gone', methods=['DELETE'])
    def gone():
        return Response(status=204)

    return app


def _asgi(asgi_app, method, path, headers=()):
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': method, 'scheme': 'http', 'path': path,
        'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
        'headers': [(b'host', b'127.0.0.1:5000')] + list(headers),
        'client': LOCAL_PEER, 'server': ('127.0.0.1', 5000),
    }
    sent = []
    chunks = [{'type': 'http.request', 'body': b'', 'more_body': False}]

    async def receive():
        return chunks.pop(0) if chunks else {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi_app(scope, receive, send))
    return sent


def _started(sent):
    starts = [m for m in sent if m['type'] == 'http.response.start']
    assert len(starts) == 1, 'exactly one response start, got %r' % (sent,)
    body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
    return starts[0], body


@pytest.fixture
def app():
    pytest.importorskip('hypercorn')
    from core.serve import build_asgi_app
    return build_asgi_app(_asset_app())


def test_a_fresh_get_answers_200_with_the_bytes(app):
    start, body = _started(_asgi(app, 'GET', '/asset'))
    assert start['status'] == 200 and body == b'bytes'


def test_a_revalidation_answers_304_not_500(app):
    """The browser holds the asset and asks whether it changed: 304, empty."""
    start, body = _started(_asgi(app, 'GET', '/asset', [(b'if-none-match', b'"v1"')]))
    assert start['status'] == 304
    assert body == b''
    assert (b'etag', b'"v1"') in start['headers']


def test_a_head_answers_200_with_no_body(app):
    """Werkzeug answers HEAD for a GET route with the headers and no body."""
    start, body = _started(_asgi(app, 'HEAD', '/asset'))
    assert start['status'] == 200 and body == b''


def test_a_204_starts(app):
    start, body = _started(_asgi(app, 'DELETE', '/gone'))
    assert start['status'] == 204 and body == b''


def test_the_start_goes_out_once_for_a_body_with_chunks(app):
    """The fix must not double-start a normal response: one start, then body."""
    sent = _asgi(app, 'GET', '/asset')
    kinds = [m['type'] for m in sent]
    assert kinds.count('http.response.start') == 1
    assert kinds.index('http.response.start') < kinds.index('http.response.body')
