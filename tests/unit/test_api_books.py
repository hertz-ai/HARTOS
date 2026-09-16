"""The book routes every HARTOS node serves (integrations/learning/api_books.py).

The real blueprint on a real Flask app, over the real pipeline and models on a
real SQLite file. Stubbed: the vision model, the title LLM, the publisher and
the rate limiter's backend.

    python -m pytest tests/unit/test_api_books.py -q --noconftest
"""
import io
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('pypdfium2')

from flask import Flask  # noqa: E402

from tests.unit.book_fixtures import make_pdf  # noqa: E402

# book_db / book_uploads / book_node: the real library on a temp SQLite file.
pytest_plugins = ['tests.unit.book_fixtures']

LOCAL = {'REMOTE_ADDR': '127.0.0.1'}
REMOTE = {'REMOTE_ADDR': '10.9.8.7'}


@pytest.fixture
def client(book_node, monkeypatch):
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    monkeypatch.delenv('HEVOLVE_TRUST_KONG', raising=False)
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)
    limiter = MagicMock()
    limiter.check.return_value = True
    limiter.get_retry_after.return_value = 60
    monkeypatch.setattr('security.rate_limiter_redis.get_rate_limiter', lambda: limiter)
    from integrations.learning.api_books import books_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(books_bp)
    c = app.test_client()
    c.limiter = limiter
    return c


@pytest.fixture
def pdf_bytes(tmp_path):
    return make_pdf(tmp_path / 'src.pdf').read_bytes()


def _remote_user(user_id='u-remote'):
    return patch('integrations.social.auth._get_user_from_token',
                 return_value=(MagicMock(id=user_id, is_banned=False), MagicMock()))


def _upload(client, data, name='book.pdf', env=LOCAL, headers=None, user_id='42'):
    form = {'file': (io.BytesIO(data), name), 'user_id': user_id, 'request_id': 'req-1'}
    return client.post('/upload/parse_pdf', data=form, content_type='multipart/form-data',
                       environ_base=env, headers=headers or {})


def _wait(client, job_id, env=LOCAL, headers=None):
    deadline = time.time() + 60
    while time.time() < deadline:
        body = client.get(f'/upload/parse_pdf/status?job_id={job_id}',
                          environ_base=env, headers=headers or {}).get_json()
        if body.get('status') in ('completed', 'failed'):
            return body
        time.sleep(0.1)
    raise AssertionError('the parse did not finish')


class TestTheDesktopFlow:
    """What the web client does: multipart upload, no session, reads
    request_id and file_url from the response (Demopage.js)."""

    def test_upload_returns_202_with_what_the_web_client_reads(self, client, pdf_bytes):
        r = _upload(client, pdf_bytes)
        assert r.status_code == 202
        body = r.get_json()
        assert body['request_id'] == 'req-1'
        assert body['file_url'].startswith('/uploads/files/') and body['file_url'].endswith('.pdf')
        assert body['job_id'] and body['file_id']
        done = _wait(client, body['job_id'])
        assert done['status'] == 'completed'
        assert done['result']['total_pages'] == 3

    def test_the_parsed_book_is_listed_and_readable(self, client, pdf_bytes):
        body = _upload(client, pdf_bytes).get_json()
        _wait(client, body['job_id'])
        books = client.get('/db/pdf_files?user_id=42', environ_base=LOCAL).get_json()
        assert [b['file_id'] for b in books] == [body['file_id']]
        assert books[0]['status'] == 'completed' and 'text_response' not in books[0]
        assert 'directory' not in books[0]       # the server's own path is no caller's business
        rows = client.get(f"/db/layouts?file_id={body['file_id']}", environ_base=LOCAL).get_json()
        assert [r['page_number'] for r in rows] == [1, 2, 3]
        one = client.get(f"/db/layouts?file_id={body['file_id']}&page_number=2",
                         environ_base=LOCAL).get_json()
        assert [r['page_number'] for r in one] == [2]

    def test_a_page_image_is_served(self, client, pdf_bytes):
        body = _upload(client, pdf_bytes).get_json()
        _wait(client, body['job_id'])
        r = client.get(f"/uploads/pdf_parse/{body['file_id']}/page_1.jpg", environ_base=LOCAL)
        assert r.status_code == 200 and r.mimetype == 'image/jpeg'
        cache = r.headers['Cache-Control']
        assert 'private' in cache and 'public' not in cache   # one user's page
        assert client.get(f"/uploads/pdf_parse/{body['file_id']}/page_9.jpg",
                          environ_base=LOCAL).status_code == 404

    def test_an_already_uploaded_file_can_be_named_by_url(self, client, pdf_bytes):
        first = _upload(client, pdf_bytes).get_json()
        _wait(client, first['job_id'])
        again = client.post('/upload/parse_pdf', json={'file_url': first['file_url'],
                                                       'user_id': '42'}, environ_base=LOCAL)
        assert again.status_code == 202
        assert again.get_json()['file_id'] == first['file_id']      # not parsed twice


class TestBadInput:

    def test_no_file(self, client):
        r = client.post('/upload/parse_pdf', data={}, content_type='multipart/form-data',
                        environ_base=LOCAL)
        assert r.status_code == 400 and 'No file' in r.get_json()['error']

    def test_not_a_pdf_by_name(self, client, pdf_bytes):
        r = _upload(client, pdf_bytes, name='book.png')
        assert r.status_code == 400 and 'Only PDF' in r.get_json()['error']

    def test_not_a_pdf_by_content(self, client):
        r = _upload(client, b'<html>not a pdf</html>')
        assert r.status_code == 400 and 'not a PDF' in r.get_json()['error']

    @pytest.mark.parametrize('url,code', [('https://example.com/x.pdf', 400),
                                          ('/uploads/../../etc/passwd', 404),
                                          ('/uploads/files/missing.pdf', 404)])
    def test_a_file_url_must_be_an_existing_upload(self, client, url, code):
        r = client.post('/upload/parse_pdf', json={'file_url': url}, environ_base=LOCAL)
        assert r.status_code == code

    def test_layouts_need_a_file_id(self, client):
        assert client.get('/db/layouts', environ_base=LOCAL).status_code == 400

    def test_an_unknown_job(self, client):
        assert client.get('/upload/parse_pdf/status?job_id=nope',
                          environ_base=LOCAL).status_code == 404

    def test_uploads_are_rate_limited(self, client, pdf_bytes):
        client.limiter.check.return_value = False
        assert _upload(client, pdf_bytes).status_code == 429

    def test_a_node_out_of_disk_answers_507_and_keeps_no_partial_file(
            self, client, pdf_bytes, book_uploads, monkeypatch):
        real_write = Path.write_bytes

        def disk_full(self, data):
            real_write(self, data[:10])
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(Path, 'write_bytes', disk_full)
        assert _upload(client, pdf_bytes).status_code == 507
        assert list((book_uploads / 'files').glob('*.pdf')) == []


class TestOverTheNetwork:
    """Any node serves these routes now, so a remote caller must be a user,
    and only ever sees their own books."""

    @pytest.mark.parametrize('method,url', [
        ('post', '/upload/parse_pdf'), ('get', '/upload/parse_pdf/status?job_id=x'),
        ('get', '/db/pdf_files'), ('get', '/db/layouts?file_id=1'),
        ('get', '/uploads/pdf_parse/1/page_1.jpg'),
    ])
    def test_an_anonymous_remote_caller_is_refused(self, client, method, url):
        assert getattr(client, method)(url, environ_base=REMOTE).status_code == 401

    def test_a_remote_user_is_scoped_to_their_own_books(self, client, pdf_bytes, tmp_path):
        mine = _upload(client, pdf_bytes).get_json()                 # local user 42
        _wait(client, mine['job_id'])

        auth = {'Authorization': 'Bearer tok'}
        other_pdf = make_pdf(tmp_path / 'other.pdf', outline=None).read_bytes()
        with _remote_user('u-remote'):
            theirs = _upload(client, other_pdf, env=REMOTE, headers=auth, user_id='42').get_json()
            _wait(client, theirs['job_id'], env=REMOTE, headers=auth)
            listed = client.get('/db/pdf_files?user_id=42', environ_base=REMOTE,
                                headers=auth).get_json()
            layouts = client.get(f"/db/layouts?file_id={mine['file_id']}",
                                 environ_base=REMOTE, headers=auth).get_json()
            image = client.get(f"/uploads/pdf_parse/{mine['file_id']}/page_1.jpg",
                               environ_base=REMOTE, headers=auth)
            status = client.get(f"/upload/parse_pdf/status?job_id={mine['job_id']}",
                                environ_base=REMOTE, headers=auth)

        # The form said user 42; the token said u-remote. The token wins.
        assert [(b['file_id'], b['user_id']) for b in listed] == [(theirs['file_id'], 'u-remote')]
        assert layouts == []
        assert image.status_code == 404
        assert status.status_code == 404
