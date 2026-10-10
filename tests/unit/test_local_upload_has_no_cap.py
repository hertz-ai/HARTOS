"""A local caller's upload has no size cap; every other caller keeps it.

Owner, 2026-10-10: "local need not have a cap".  Measured live on the desktop
that day: a 1.9 MB request body reached the app, 2.1 MB got an empty 400 from
the transport, 3 MB had its connection dropped, and a 2.5 MB avatar image to
/upload/image got the same empty 400.  So a book uploaded from the browser, an
avatar photo or a voice recording over 2 MB could never arrive.

The caps that apply to a request body, and what each test drives:
  * the transport (core.serve.build_asgi_app, Hypercorn's WSGI middleware):
    the real middleware, called as an ASGI app;
  * Flask (hart_intelligence_entry's app, MAX_CONTENT_LENGTH): the real app;
  * the book route's PDF size (MAX_PDF_BYTES): the real route and pipeline.
"Local" is core.auth_local's rule: a loopback socket peer, and, through a
proxy on this machine, a loopback client too.  A remote caller keeps every cap.
"""
import asyncio
import io
from unittest.mock import MagicMock

import pytest

MB = 1024 * 1024
LOCAL_PEER = ('127.0.0.1', 50000)
REMOTE_PEER = ('10.9.8.7', 50000)


# ── The transport ────────────────────────────────────────────────────

def _echo_app():
    """A WSGI app that reads the whole body and answers its length."""
    from flask import Flask, request
    app = Flask(__name__)

    @app.route('/echo', methods=['POST'])
    def echo():
        return str(len(request.get_data()))

    return app


def _asgi_post(asgi_app, body: bytes, client, headers=()):
    """POST `body` through the ASGI app; (status, response body)."""
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'POST', 'scheme': 'http', 'path': '/echo',
        'raw_path': b'/echo', 'query_string': b'', 'root_path': '',
        'headers': [(b'host', b'127.0.0.1:5000'),
                    (b'content-length', str(len(body)).encode())] + list(headers),
        'client': client, 'server': ('127.0.0.1', 5000),
    }
    sent = []
    chunks = [{'type': 'http.request', 'body': body, 'more_body': False}]

    async def receive():
        return chunks.pop(0) if chunks else {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi_app(scope, receive, send))
    start = next(m for m in sent if m['type'] == 'http.response.start')
    text = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
    return start['status'], text


class TestTheTransport:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        pytest.importorskip('hypercorn')
        monkeypatch.delenv('TRUSTED_PROXY', raising=False)
        monkeypatch.delenv('NUNBA_CI', raising=False)

    def _app(self):
        from core.serve import build_asgi_app
        return build_asgi_app(_echo_app())

    def test_a_local_body_over_the_cap_reaches_the_app(self):
        status, text = _asgi_post(self._app(), b'x' * (3 * MB), LOCAL_PEER)
        assert (status, text) == (200, str(3 * MB).encode())

    def test_a_remote_body_over_the_cap_is_refused(self):
        status, _ = _asgi_post(self._app(), b'x' * (3 * MB), REMOTE_PEER)
        assert status == 400

    def test_a_remote_client_through_a_local_proxy_is_refused(self):
        """A proxy on this machine (loopback peer) forwarding a LAN client:
        the client it names is not local, so the cap holds."""
        status, _ = _asgi_post(self._app(), b'x' * (3 * MB), LOCAL_PEER,
                               headers=[(b'x-forwarded-for', b'192.168.1.20')])
        assert status == 400

    def test_a_remote_body_under_the_cap_still_reaches_the_app(self):
        status, text = _asgi_post(self._app(), b'x' * MB, REMOTE_PEER)
        assert (status, text) == (200, str(MB).encode())


# ── Flask ────────────────────────────────────────────────────────────

class TestTheAppCap:
    """hart_intelligence_entry's own app: what Werkzeug reads as the cap
    when it opens the body stream or parses a form."""

    @pytest.fixture(scope='class')
    def app(self):
        try:
            from hart_intelligence_entry import app
        except Exception as e:     # an interpreter that cannot import the entry
            pytest.skip(f'hart_intelligence_entry does not import here: {e}')
        return app

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.delenv('TRUSTED_PROXY', raising=False)
        monkeypatch.delenv('NUNBA_CI', raising=False)

    def _cap(self, app, remote_addr, forwarded=None):
        from flask import request
        headers = {'X-Forwarded-For': forwarded} if forwarded else {}
        with app.test_request_context('/', method='POST', headers=headers,
                                      environ_base={'REMOTE_ADDR': remote_addr}):
            return request.max_content_length

    def test_a_local_request_has_no_cap(self, app):
        from core.constants import MAX_PAYLOAD_BYTES
        assert self._cap(app, '127.0.0.1') > 100 * MAX_PAYLOAD_BYTES

    def test_a_remote_request_keeps_the_cap(self, app):
        from core.constants import MAX_PAYLOAD_BYTES
        assert self._cap(app, '10.9.8.7') == MAX_PAYLOAD_BYTES

    def test_a_forwarded_remote_client_keeps_the_cap(self, app):
        from core.constants import MAX_PAYLOAD_BYTES
        assert self._cap(app, '127.0.0.1', forwarded='192.168.1.20') == MAX_PAYLOAD_BYTES


# ── The book route ───────────────────────────────────────────────────

pytest_plugins = ['tests.unit.book_fixtures']

LOCAL = {'REMOTE_ADDR': '127.0.0.1'}
REMOTE = {'REMOTE_ADDR': '10.9.8.7'}


class TestTheBookRoute:
    @pytest.fixture
    def client(self, book_node, monkeypatch):
        pytest.importorskip('pypdfium2')
        from flask import Flask
        monkeypatch.delenv('TRUSTED_PROXY', raising=False)
        monkeypatch.delenv('NUNBA_CI', raising=False)
        limiter = MagicMock()
        limiter.check.return_value = True
        monkeypatch.setattr('security.rate_limiter_redis.get_rate_limiter',
                            lambda: limiter)
        from integrations.learning.api_books import books_bp
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(books_bp)
        return app.test_client()

    @pytest.fixture
    def pdf(self, tmp_path):
        from tests.unit.book_fixtures import make_pdf
        return make_pdf(tmp_path / 'src.pdf').read_bytes()

    def _upload(self, client, data, env, headers=None):
        form = {'file': (io.BytesIO(data), 'book.pdf'), 'user_id': '42',
                'request_id': 'req-1'}
        return client.post('/upload/parse_pdf', data=form,
                           content_type='multipart/form-data',
                           environ_base=env, headers=headers or {})

    def test_a_local_book_over_max_pdf_bytes_is_taken(self, client, pdf, monkeypatch):
        from integrations.learning import book_pipeline as bp
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', len(pdf) - 1)
        r = self._upload(client, pdf, LOCAL)
        assert r.status_code == 202, r.get_json()

    def test_a_remote_book_over_max_pdf_bytes_is_refused(self, client, pdf, monkeypatch):
        from unittest.mock import patch

        from integrations.learning import book_pipeline as bp
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', len(pdf) - 1)
        user = (MagicMock(id='u-remote', is_banned=False), MagicMock())
        with patch('integrations.social.auth._get_user_from_token', return_value=user):
            r = self._upload(client, pdf, REMOTE, {'Authorization': 'Bearer tok'})
        assert r.status_code == 400 and 'larger than' in r.get_json()['error']

    def test_a_download_keeps_its_cap(self, monkeypatch):
        """fetch_pdf saves through save_pdf's default: MAX_PDF_BYTES."""
        from integrations.learning import book_pipeline as bp
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', 10)
        with pytest.raises(bp.BookParseError, match='larger than'):
            bp.save_pdf(b'%PDF-1.4 ' + b'x' * 50, 'x.pdf')
