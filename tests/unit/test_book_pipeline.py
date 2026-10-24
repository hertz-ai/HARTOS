"""The book pipeline (integrations/learning/book_pipeline.py), driven for real.

Real PDFs (tests/unit/book_fixtures.py), real pypdfium2 rendering, text and
outline, the real models on a real SQLite file, the real orchestrator.
Stubbed only at the boundaries: the vision model, the title LLM and the
client publisher.

    python -m pytest tests/unit/test_book_pipeline.py -q --noconftest
"""
import itertools
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('pypdfium2')

from integrations.learning import book_pipeline as bp  # noqa: E402
from tests.unit.book_fixtures import (  # noqa: E402
    PAGES, make_encrypted_pdf, make_nested_outline_pdf, make_pdf,
)

# book_db / book_uploads / book_node: the real library on a temp SQLite file.
pytest_plugins = ['tests.unit.book_fixtures']


def _by_page(file_id, field='passage'):
    out = {}
    for row in bp.get_layouts(file_id):
        out.setdefault(row['page_number'], []).append(row[field])
    return out


#: What pdf_files.user_id holds. Spelled out here rather than read from
#: bp.READER_ID_MAX so the tests below fail on a PARENT by the behaviour they
#: claim, instead of by AttributeError on a constant the parent does not have
#: (that review's F8). test_the_reader_id_limit_matches_its_column ties the two
#: together, so this literal cannot drift from the column either.
READER_ID_CHARS = 64


def _row(file_id):
    return bp.get_book(file_id)


def _stored_status(file_id):
    """The status ON THE ROW, not the one a read computes.

    _row goes through get_book, which runs _stall_reason and shows a dead
    parse as 'failed' whether or not the record was ever written -- so an
    assertion on _row cannot tell "recorded" from "merely shown".  That is
    how a recorder ignoring the job survived every test (A4/M8 of the
    1faa4b6a0 review), including the two whose own comments claimed to check
    exactly this.
    """
    from integrations.social.models import BookFile, db_session
    with db_session(commit=False) as db:
        row = db.get(BookFile, int(file_id))
        return row.status if row is not None else None


def _only_book():
    books = bp.list_books()
    assert len(books) == 1, books
    return books[0]


def _wait_for_job(job_id, seconds=60):
    deadline = time.time() + seconds
    job = bp.get_job(job_id)
    while job['status'] not in ('completed', 'failed') and time.time() < deadline:
        time.sleep(0.1)
        job = bp.get_job(job_id)
    return job


# ── A node with NO vision model still produces a readable book ─────────

class TestTextLayer:

    def test_every_page_is_stored_from_its_text_layer(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42', 'req-1')
        assert result['total_pages'] == 3
        book = _row(result['file_id'])
        assert book['status'] == 'completed' and book['total_pages'] == 3
        text = _by_page(result['file_id'])
        assert 'The SI base unit of length is the metre.' in text[1][0]
        assert 'Acceleration is the rate of change of velocity.' in text[3][0]

    def test_chapters_come_from_the_outline(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        chapters = {p: v[0] for p, v in _by_page(result['file_id'], 'chapter_name').items()}
        assert chapters == {1: 'Chapter 1: Units and Measurement',
                            2: 'Chapter 1: Units and Measurement',
                            3: 'Chapter 2: Motion'}

    def test_outline_sections_become_topics(self, book_node, tmp_path):
        result = bp.parse_book(make_nested_outline_pdf(tmp_path / 'nested.pdf'), '42')
        topics = {p: v[0] for p, v in _by_page(result['file_id'], 'topic_name').items()}
        chapters = {p: v[0] for p, v in _by_page(result['file_id'], 'chapter_name').items()}
        assert topics == {1: '1.1 Base units', 2: '1.2 Dimensions', 3: None}
        assert chapters[3] == 'Chapter 2: Motion'

    def test_a_pdf_without_an_outline_still_serves_every_page(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'flat.pdf', outline=None), '42')
        chapters = _by_page(result['file_id'], 'chapter_name')
        assert chapters == {1: [None], 2: [None], 3: [None]}

    def test_the_metadata_title_names_the_book_when_no_llm_answers(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'b.pdf', title='NCERT Physics Part I'), '42')
        assert _row(result['file_id'])['book_name'] == 'NCERT Physics Part I'


# ── Page images: what the agent quotes ────────────────────────────────

class TestPageImages:

    def test_a_page_image_is_never_served_half_written(self, tmp_path):
        """_write_jpeg's claim: it writes under a temporary name and moves it
        into place, "so the page-image route never serves a half-written
        file".  Nothing held that -- saving straight to the destination passed
        every test (cc12's docstring sweep of 74b39d1a4).
        """
        out = tmp_path / 'page_1.jpg'
        seen = {}

        class OnePage:
            def save(self, target, *args, **kwargs):
                seen['target'] = str(target)
                seen['destination_existed'] = out.exists()
                Path(target).write_bytes(b'jpeg-bytes')

        bp._write_jpeg(OnePage(), out)

        assert seen['target'].endswith('.part')      # not the served name
        assert seen['destination_existed'] is False  # nothing to serve half-written
        assert out.read_bytes() == b'jpeg-bytes'
        assert not out.with_name(out.name + '.part').exists()

    def test_every_page_gets_an_image(self, book_node, tmp_path):
        from PIL import Image
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        for page in (1, 2, 3):
            path = bp.page_image_path(result['file_id'], page)
            assert path.is_file()
            with Image.open(path) as im:
                assert max(im.size) <= bp.MAX_RENDER_PX
                assert im.size == (1700, 2200)      # a letter page at 200 DPI
        assert bp.page_image_url(result['file_id'], 2) == \
            f"/uploads/pdf_parse/{result['file_id']}/page_2.jpg"

    def test_a_page_claiming_to_be_200_inches_wide_renders_small(self, book_node, tmp_path):
        """Uncapped, this page is a 40,000 x 40,000 bitmap (~6 GB)."""
        from PIL import Image
        pdf = make_pdf(tmp_path / 'giant.pdf', pages=PAGES[:1], outline=None,
                       media_box=(14400, 14400))
        result = bp.parse_book(pdf, '42')
        with Image.open(bp.page_image_path(result['file_id'], 1)) as im:
            assert max(im.size) <= bp.MAX_RENDER_PX


# ── The vision model: one call per page, text layer as the fallback ────

def _in_page_order(reply):
    """A vision backend answering page N with reply(N): pages are read in order."""
    pages = itertools.count(1)
    return lambda image, prompt: reply(next(pages))


class TestVision:

    def test_the_model_reads_the_page_when_it_answers(self, book_node, tmp_path):
        book_node.vision.side_effect = _in_page_order(lambda n: json.dumps({
            'page_type': 'content', 'text': f'VLM page {n}',
            'elements': [{'type': 'paragraph', 'content': f'VLM para {n}'}]}))
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert _by_page(result['file_id']) == {1: ['VLM para 1'], 2: ['VLM para 2'],
                                              3: ['VLM para 3']}
        assert 'VLM page 1' in result['whole_text']
        # The backend is handed the rendered page, a JPEG, with the page prompt.
        image, prompt = book_node.vision.call_args_list[0].args
        assert image[:3] == b'\xff\xd8\xff' and prompt == bp._PAGE_PROMPT

    def test_the_models_toc_names_chapters_when_the_pdf_has_no_outline(self, book_node, tmp_path):
        def vision(n):
            reply = {'page_type': 'content', 'text': f'page {n}', 'elements': []}
            if n == 1:
                reply['toc_entries'] = [{'title': 'Kinematics', 'page': 3}]
            return json.dumps(reply)
        book_node.vision.side_effect = _in_page_order(vision)
        result = bp.parse_book(make_pdf(tmp_path / 'flat.pdf', outline=None), '42')
        chapters = {p: v[0] for p, v in _by_page(result['file_id'], 'chapter_name').items()}
        assert chapters == {1: None, 2: None, 3: 'Kinematics'}

    def test_a_model_that_never_answers_is_asked_only_twice(self, book_node, tmp_path):
        bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert book_node.vision.call_count == bp._VLM_GIVE_UP_AFTER

    def test_a_blank_answer_for_one_page_falls_back_for_that_page_only(self, book_node, tmp_path):
        book_node.vision.side_effect = _in_page_order(lambda n: json.dumps(
            {'page_type': 'content', 'elements': [],
             'text': '' if n == 2 else f'VLM page {n}'}))
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        text = _by_page(result['file_id'])
        assert text[1] == ['VLM page 1'] and text[3] == ['VLM page 3']
        assert 'Dimensional analysis checks equations.' in text[2][0]
        assert book_node.vision.call_count == 3

    def test_a_vision_package_that_will_not_import_leaves_the_text_layer(
            self, book_node, tmp_path, monkeypatch):
        """The same as no model: pages come from the text layer, the book is not failed."""
        monkeypatch.setitem(sys.modules, 'integrations.vision.lightweight_backend', None)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert _row(result['file_id'])['status'] == 'completed'
        assert 'Acceleration is the rate of change of velocity.' in \
            _by_page(result['file_id'])[3][0]
        book_node.vision.assert_not_called()

    def test_a_backend_that_raises_leaves_the_text_layer(self, book_node, tmp_path):
        """A backend failing mid-read (out of memory, say) fails no book."""
        book_node.vision.side_effect = RuntimeError('CUDA out of memory')
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert _row(result['file_id'])['status'] == 'completed'
        assert 'Acceleration is the rate of change of velocity.' in \
            _by_page(result['file_id'])[3][0]


# ── Failures are honest and durable ───────────────────────────────────

class TestFailures:

    def _fails(self, pdf, match):
        with pytest.raises(bp.BookParseError, match=match):
            bp.parse_book(pdf, '42')
        book = _only_book()
        assert book['status'] == 'failed'
        assert book['error']
        return book

    def test_a_file_that_is_not_a_pdf(self, book_node, tmp_path):
        bad = tmp_path / 'not_a.pdf'
        bad.write_bytes(b'this is not a pdf')
        self._fails(bad, 'could not be read')

    def test_a_file_that_is_gone_does_not_hand_back_its_path(
            self, book_node, tmp_path, caplog):
        """The uploads path leaves this node, so it must not be in the answer.

        book_tools puts a failed book's error into an agent's tool output,
        which becomes a model's prompt and that model can be a cloud one, and
        api_books hands the job's error to a client that may be a phone. The
        path carries the OS user's name (H-0715: scrubbing is for what leaves,
        not for the operator's own log). The operator still gets both, below.
        """
        missing = tmp_path / 'a_book.pdf'
        with caplog.at_level('WARNING'):
            with pytest.raises(bp.BookParseError) as raised:
                bp._open(missing)

        said = str(raised.value)
        # The DIRECTORY is the sensitive half: it carries the OS user's name.
        # The filename is not secret -- the row publishes it in `filename` and
        # in `book_name` -- so a fixture called MySecretDiary.pdf implied a
        # confidentiality this code does not offer (that review's F6).
        assert str(tmp_path) not in said
        # and the operator loses nothing: the full path AND a traceback
        opened = [r for r in caplog.records if 'could not be opened' in r.getMessage()]
        assert opened and str(tmp_path) in opened[0].getMessage()
        assert opened[0].exc_info is not None

    def test_a_file_that_is_gone_does_not_say_none(self, book_node, tmp_path):
        """The case a naive scrub gets wrong, which is in fact the only case.

        pypdfium2 raises FileNotFoundError(path): str(e) is the path and
        nothing else, and strerror, filename and errno are all None (measured
        2026-10-25). So reporting e.strerror by itself would tell a person
        "the PDF could not be opened (None)" -- flagged by hartos-70 before
        this was written, and true of every OSError this branch sees.
        """
        with pytest.raises(bp.BookParseError) as raised:
            bp._open(tmp_path / 'gone.pdf')
        said = str(raised.value)
        assert 'None' not in said
        assert 'no longer on this node' in said

    def test_a_file_that_is_not_a_pdf_still_says_what_pdfium_said(
            self, book_node, tmp_path):
        """The guard against over-scrubbing the branch beside it.

        PDFium's text names no path, so it is still repeated in full; losing
        it would leave a person "this file could not be read as a PDF ()".
        """
        bad = tmp_path / 'not_a.pdf'
        bad.write_bytes(b'this is plainly not a pdf')
        with pytest.raises(bp.BookParseError) as raised:
            bp._open(bad)
        assert 'format error' in str(raised.value).lower()

    def test_a_refused_account_is_not_called_an_unavailable_library(
            self, book_node, tmp_path):
        """parse_book reported "the book library is unavailable" for ANY
        registration failure, so a refused account id told a person the library
        was down while it was answering perfectly well (F1 of the 30c304dac
        review).

        01639d757 fixed that and its message claimed the change was "pinned by
        a test that reads the message".  It was not: reverting it left 169
        passing, which is what got that commit rejected.  This is the test the
        claim needed.
        """
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), 'u' * 65)

        assert result['stored'] is False
        assert 'unavailable' not in result['store_error']
        assert 'account' in result['store_error']

    def test_a_missing_file_is_not_told_to_upload_it_again(
            self, book_node, tmp_path):
        """The same branch serves a link the caller named by URL and never
        uploaded, so "please upload it again" is wrong advice there (F5 of the
        30c304dac review).

        Unpinned for a reason worth remembering: the test beside this one
        asserts "no longer on this node", which was true of the OLD text too,
        so it matched the part that did not change.  An assertion has to name
        the thing the change removed.
        """
        with pytest.raises(bp.BookParseError) as raised:
            bp._open(tmp_path / 'gone.pdf')

        assert 'upload' not in str(raised.value).lower()

    def test_a_password_protected_pdf_says_so(self, book_node, tmp_path):
        book = self._fails(make_encrypted_pdf(tmp_path / 'locked.pdf'), 'password-protected')
        assert 'password-protected' in book['error']

    def test_a_scanned_pdf_with_no_vision_model(self, book_node, tmp_path):
        pdf = make_pdf(tmp_path / 'scan.pdf', pages=[('', ''), ('', '')], outline=None,
                       raw_streams=['', ''])
        self._fails(pdf, 'no text could be read')
        assert bp.get_layouts(_only_book()['file_id']) == []      # nothing half-stored

    def test_too_many_pages(self, book_node, tmp_path, monkeypatch):
        monkeypatch.setattr(bp, 'MAX_PAGES', 2)
        self._fails(make_pdf(tmp_path / 'book.pdf'), 'at most 2')

    def test_a_book_that_cannot_be_saved_is_recorded_and_still_read_for_the_agent(
            self, book_node, tmp_path, monkeypatch):
        """The agent's synchronous read keeps the text, as its PDF reader
        always did; the library records the failure."""
        monkeypatch.setattr(bp, '_save', MagicMock(side_effect=RuntimeError('disk full')))
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert result['stored'] is False and 'disk full' in result['store_error']
        assert 'Acceleration is the rate of change of velocity.' in result['whole_text']
        book = _only_book()
        assert book['status'] == 'failed' and 'could not be saved' in book['error']

    def test_a_background_parse_that_cannot_be_saved_fails(self, book_node, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(bp, '_save', MagicMock(side_effect=RuntimeError('disk full')))
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42')
        job = _wait_for_job(started['job_id'])
        assert job['status'] == 'failed' and 'disk full' in job['error']
        assert _row(started['file_id'])['status'] == 'failed'

    def test_the_agent_still_gets_the_text_when_the_library_cannot_be_written(
            self, book_node, tmp_path, monkeypatch):
        monkeypatch.setattr(bp, '_register', MagicMock(side_effect=RuntimeError('disk I/O error')))
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert result['stored'] is False and 'unavailable' in result['store_error']
        assert 'Acceleration is the rate of change of velocity.' in result['whole_text']
        assert bp.list_books('42') == []

    def _db_error(self, statement, params, message='disk I/O error'):
        """A real SQLAlchemy failure: str() renders the statement and the
        parameters into it, which is the whole of this defect."""
        from sqlalchemy.exc import OperationalError
        return OperationalError(statement, params, sqlite3.OperationalError(message))

    def test_a_save_that_fails_does_not_hand_back_the_statement(
            self, book_node, tmp_path, monkeypatch):
        """The person's copy of a non-lock database failure.

        A held lock has been answered safely since 2026-10-18, but that fix
        sorted failures by transience and left the rendering alone: anything
        NOT a lock still took the else branch and printed the statement. For
        _save the parameters are the executemany payload -- the book's own
        page text -- and this node really did raise 'disk I/O error' all
        afternoon on 2026-10-22, with the disk at zero bytes free.
        """
        boom = self._db_error('DELETE FROM page_layouts WHERE file_id = ?',
                              {'passage': "a page of the book's own text"})
        monkeypatch.setattr(bp, '_save', MagicMock(side_effect=boom))

        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        said = result['store_error']
        assert result['stored'] is False
        assert said != bp._BUSY_NOT_SAVED, 'a disk error is not the library being busy'
        assert 'disk I/O error' in said, 'the person is still told what went wrong'
        assert '[SQL:' not in said and '[parameters:' not in said, said
        assert 'page_layouts' not in said and 'own text' not in said, said

    def test_a_library_that_cannot_be_reached_does_not_hand_back_the_path(
            self, book_node, tmp_path, monkeypatch):
        """parse_book's registration boundary (hartos-94's M1).

        _register's parameters carry the uploads directory, so the rendered
        statement hands a server path to whoever reads the agent's progress.
        """
        boom = self._db_error(
            'INSERT INTO pdf_files (user_id, filename, directory) VALUES (?, ?, ?)',
            {'directory': r'C:\Users\sathi\Documents\Nunba\uploads\pdf_parse'})
        monkeypatch.setattr(bp, '_register', MagicMock(side_effect=boom))

        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        said = result['store_error']
        assert result['stored'] is False and 'unavailable' in said
        assert 'disk I/O error' in said
        assert '[SQL:' not in said and '[parameters:' not in said, said
        assert 'Documents' not in said and 'pdf_files' not in said, said
        assert 'Acceleration is the rate of change of velocity.' in result['whole_text']

    def test_a_background_parse_does_not_put_the_statement_in_its_job(
            self, book_node, tmp_path, monkeypatch):
        """The same leak on the route a person can poll.

        The text measured on 2026-10-18 and quoted beside _BUSY_NOT_SAVED
        was built here, by 'unexpected error: {e}'. The lock half of that
        line was closed; this is the half that was left.
        """
        boom = self._db_error('SELECT pdf_files.file_id FROM pdf_files', {'file_id': 1})
        monkeypatch.setattr(bp, '_parse', MagicMock(side_effect=boom))

        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42')
        job = _wait_for_job(started['job_id'])

        assert job['status'] == 'failed'
        assert 'disk I/O error' in job['error']
        assert '[SQL:' not in job['error'] and '[parameters:' not in job['error']
        assert _row(started['file_id'])['error'] == job['error']

    def _logged_exceptions(self, caplog):
        return [r for r in caplog.records if r.exc_info]

    def test_the_operator_still_gets_the_statement_when_a_save_fails(
            self, book_node, tmp_path, monkeypatch, caplog):
        """Sanitising the person's copy must not blind the operator.

        acd5f5a42 replaced the driver's text in `reason` and logged only
        `reason`, so on the agent's synchronous path a disk I/O error left
        the operator four words and no stack -- the parent had the statement,
        inside the reason it printed. Found on review, not by a test, because
        no test had ever read the log on this path.
        """
        import logging
        boom = self._db_error('DELETE FROM page_layouts WHERE file_id = ?',
                              {'passage': "a page of the book's own text"})
        monkeypatch.setattr(bp, '_save', MagicMock(side_effect=boom))

        with caplog.at_level(logging.WARNING, logger=bp.logger.name):
            result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        assert '[SQL:' not in result['store_error']        # the person's copy
        logged = self._logged_exceptions(caplog)
        assert logged, 'the operator was left the sanitised sentence alone'
        full = '\n'.join(str(r.exc_info[1]) for r in logged)
        assert '[SQL:' in full and 'page_layouts' in full  # the operator's copy

    def test_the_operator_still_gets_the_statement_when_registration_fails(
            self, book_node, tmp_path, monkeypatch, caplog):
        """parse_book's registration boundary, the same omission."""
        import logging
        boom = self._db_error('INSERT INTO pdf_files (directory) VALUES (?)',
                              {'directory': '/srv/nunba/uploads/pdf_parse'})
        monkeypatch.setattr(bp, '_register', MagicMock(side_effect=boom))

        with caplog.at_level(logging.WARNING, logger=bp.logger.name):
            result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        assert '[SQL:' not in result['store_error']
        logged = self._logged_exceptions(caplog)
        assert logged, 'the operator was left the sanitised sentence alone'
        full = '\n'.join(str(r.exc_info[1]) for r in logged)
        assert '[SQL:' in full and 'pdf_files' in full

    def test_driver_text_drops_the_statement_and_keeps_the_drivers_words(self):
        boom = self._db_error('INSERT INTO pdf_files (directory) VALUES (?)',
                              {'directory': '/srv/nunba/uploads'})
        assert '[SQL:' in str(boom), 'the hazard this guards is gone'
        assert bp.driver_text(boom) == 'disk I/O error'

    def test_driver_text_with_no_driver_error_does_not_fall_back_to_the_statement(self):
        """orig None: str(exc) IS the statement, so it must not be the answer.

        This branch survived as a mutant on the acd5f5a42 review -- 122 tests
        passed with it reverted.
        """
        from sqlalchemy.exc import StatementError
        exc = StatementError('boom', 'SELECT secret FROM pdf_files', {}, None)
        assert '[SQL:' in str(exc)
        said = bp.driver_text(exc)
        assert said == 'StatementError'
        assert '[SQL:' not in said and 'pdf_files' not in said

    def test_driver_text_leaves_an_exception_that_renders_no_statement_alone(self):
        """The isinstance(StatementError) gate, the other surviving mutant.

        Only that class appends [SQL:]/[parameters:]; everything else already
        says exactly what it was given, and a book's own complaint must reach
        the person whole.
        """
        from sqlalchemy.exc import InvalidRequestError
        assert bp.driver_text(RuntimeError('the PDF could not be opened')) == (
            'the PDF could not be opened')
        assert bp.driver_text(InvalidRequestError('no such table')) == 'no such table'

    def test_driver_text_does_not_raise_when_the_driver_error_cannot_be_read(self):
        """It runs inside except blocks; raising here costs the person the answer."""
        from sqlalchemy.exc import StatementError

        class Unreadable(Exception):
            def __str__(self):
                raise ValueError('cannot be rendered')

        exc = StatementError('boom', 'SELECT 1 FROM pdf_files', {}, Unreadable())
        said = bp.driver_text(exc)
        assert said == 'StatementError'
        assert 'pdf_files' not in said

    def test_a_row_that_cannot_be_marked_processing_does_not_fail_the_book(
            self, book_node, tmp_path, monkeypatch):
        real_update, calls = bp._update_row, []

        def processing_refused(file_id, **fields):
            calls.append(fields)
            if fields == {'status': 'processing'}:
                raise RuntimeError('database is locked')
            return real_update(file_id, **fields)

        monkeypatch.setattr(bp, '_update_row', processing_refused)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert {'status': 'processing'} in calls
        assert result['stored'] is True and _row(result['file_id'])['status'] == 'completed'

    def test_a_page_whose_text_cannot_be_read_does_not_fail_the_book(
            self, book_node, tmp_path, monkeypatch):
        real_text, calls = bp._text, []

        def second_page_broken(page):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('broken content stream')
            return real_text(page)

        monkeypatch.setattr(bp, '_text', second_page_broken)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        text = _by_page(result['file_id'])
        assert text[2] == [''] and 'Acceleration' in text[3][0]

    def test_the_cve_2023_36464_stream_cannot_hang_a_parse(self, book_node, tmp_path):
        """A content stream ending in a comment with no EOL spins PyPDF2 3.0.1
        forever (reproduced 2026-09-13). The pipeline must finish, either way."""
        stream = 'BT\n/F1 18 Tf\n72 720 Td\n(Hello page) Tj\nET\n%comment-without-eol'
        pdf = make_pdf(tmp_path / 'cve.pdf', pages=[('x', 'y')], outline=None,
                       raw_streams=[stream])
        done = threading.Event()

        def run():
            try:
                bp.parse_book(pdf, '42')
            except bp.BookParseError:
                pass
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(60), 'the parse hung on the CVE-2023-36464 stream'


# ── Never on a request thread; never twice; never pending forever ─────

class TestLifecycle:

    def test_a_background_failure_is_recorded_before_the_worker_logs_it(
            self, book_node, book_db, tmp_path, monkeypatch):
        """_worker's BookParseError branch logs only debug, on the stated
        ground that the failure is "already recorded on the row and the job,
        and published".  Nothing held any of those three.
        """
        pdf = make_pdf(tmp_path / 'book.pdf')
        file_id = bp._register('42', pdf.name, pdf.parent, 'req-bg')
        job = bp._new_job(file_id, '42', 'req-bg', pdf.name)
        monkeypatch.setattr(bp, '_open', lambda *a, **k: (_ for _ in ()).throw(
            bp.BookParseError('this PDF is password-protected')))

        bp._worker(job, file_id, str(pdf), '42', 'req-bg')

        assert _stored_status(file_id) == 'failed'                      # the row
        assert bp.get_job(job)['status'] == 'failed'                    # the job
        assert any('password-protected' in str(payload)                 # and published
                   for _, payload in book_node.published)

    def test_a_background_crash_leaves_the_operator_a_traceback(
            self, book_node, monkeypatch, caplog):
        """_worker's other branch logs with logger.exception.

        _run converts everything it catches into a BookParseError, so this
        branch is only reached by a failure that escapes _run itself -- which
        is why nothing exercised it, and why driver_text's "every caller logs"
        was untested on this path (cc12's sweep).
        """
        def lost(*args, **kwargs):
            raise RuntimeError('the worker thread lost its database')
        monkeypatch.setattr(bp, '_run', lost)

        with caplog.at_level('ERROR'):
            bp._worker('job-x', 7, 'C:/tmp/x.pdf', '42', 'req-crash')

        crashed = [r for r in caplog.records
                   if 'background parse crashed' in r.getMessage()]
        assert crashed and crashed[0].exc_info is not None

    def test_a_parse_that_finished_first_is_not_overwritten_as_failed(
            self, book_node, book_db):
        """_record_stalled re-reads each row and writes "only if it is STILL
        dead, so a parse that finished a moment ago is never overwritten".
        The re-check exists for this and nothing held it: recording whatever
        the READ decided passed every test.
        """
        file_id = _a_parse_its_lock_killed('42', 'raced.pdf')
        assert bp.list_books('42')[0]['status'] == 'failed'   # the read condemns it
        bp._update_row(file_id, status='completed', error=None)   # it finishes first

        bp._resolve_stalled([file_id])

        assert _stored_status(file_id) == 'completed'

    def test_a_long_parse_keeps_its_record_fresh(self, book_node, tmp_path, monkeypatch):
        """_stall_reason condemns a row untouched for STALL_SECONDS, and the
        only thing keeping a long parse's row fresh is _run's periodic touch.
        Without it a parse running over 30 minutes is recorded FAILED mid-run.

        Measured before this was written: _run already sets status='processing'
        ONCE before the pages, so a test that merely sees one such write passes
        with the touch removed.  Hence the count rather than a flag.
        """
        monkeypatch.setattr(bp, '_TOUCH_EVERY_S', 0)      # every step touches
        touches = []
        real_update = bp._update_row

        def counting(file_id, **fields):
            if fields.get('status') == 'processing':
                touches.append(file_id)
            return real_update(file_id, **fields)

        monkeypatch.setattr(bp, '_update_row', counting)
        bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        assert len(touches) >= 2, touches

    def test_start_parse_returns_before_the_parse_runs(self, book_node, tmp_path, monkeypatch):
        release = threading.Event()
        ran = threading.Event()

        def slow_run(*args, **kwargs):
            release.wait(30)
            ran.set()

        monkeypatch.setattr(bp, '_run', slow_run)
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42', 'req-9')
        assert started['status'] == 'queued' and started['job_id'] and started['file_id']
        assert not ran.is_set(), 'start_parse must not wait for the parse'
        release.set()
        assert ran.wait(30)

    def test_a_background_parse_completes_and_reports(self, book_node, tmp_path):
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42', 'req-9')
        job = _wait_for_job(started['job_id'])
        assert job['status'] == 'completed', job
        assert job['result'] == {'file_id': started['file_id'], 'book_name': None,
                                 'total_pages': 3}
        assert _row(started['file_id'])['status'] == 'completed'

    def test_a_background_job_knows_its_page_count_before_it_finishes(
            self, book_node, tmp_path, monkeypatch):
        updates, real_update = [], bp._update_job
        monkeypatch.setattr(bp, '_update_job', lambda job_id, **fields: (
            updates.append(dict(fields)), real_update(job_id, **fields)))
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42')
        assert _wait_for_job(started['job_id'])['status'] == 'completed'
        assert {'total_pages': 3} in updates         # announced with "3 pages found"

    def test_page_images_left_under_a_reused_id_are_cleared(self, book_node, tmp_path):
        file_id = bp._register('42', 'book.pdf', str(tmp_path), '')
        stale = bp.page_image_path(file_id, 9)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b'a page of some earlier book')
        bp._run(file_id, make_pdf(tmp_path / 'book.pdf'), '42', '')
        assert not stale.exists() and bp.page_image_path(file_id, 3).is_file()

    def test_the_same_upload_is_not_parsed_twice(self, book_node, tmp_path, monkeypatch):
        monkeypatch.setattr(bp, '_worker', lambda *a, **k: None)
        pdf = make_pdf(tmp_path / 'book.pdf')
        first = bp.start_parse(pdf, '42')
        second = bp.start_parse(pdf, '42')
        assert second['file_id'] == first['file_id'] and second.get('existing') is True
        assert len(bp.list_books('42')) == 1

    def test_a_dead_parse_is_recorded_as_failed_not_left_pending(self, book_node):
        from integrations.social.models import BookFile, db_session
        file_id = bp._register('42', 'x.pdf', '/tmp', '')
        with db_session() as db:
            row = db.get(BookFile, file_id)
            row.status = 'processing'
            row.updated_at = datetime.utcnow() - timedelta(seconds=bp.STALL_SECONDS + 60)
        book = bp.list_books('42')[0]
        assert book['status'] == 'failed' and 'stalled' in book['error']
        assert _stored_status(file_id) == 'failed'          # recorded, not just shown

    def test_a_parse_only_its_job_calls_dead_is_recorded_too(self, book_node):
        """The same claim for the hot window, which nothing claimed.

        The row above is old enough for the stall timeout to condemn on its
        own, so a recorder that consulted only the timeout -- ignoring the
        job -- kept every test green (A4/M8 of the 1faa4b6a0 review).  Here
        the row was touched a moment ago and only the failed job knows, and
        it still has to be written, not merely displayed as failed.
        """
        file_id = _a_parse_its_lock_killed('42', 'hot-record.pdf')

        book = bp.list_books('42')[0]
        assert book['status'] == 'failed'
        assert _stored_status(file_id) == 'failed'      # recorded, not just shown

    def test_a_stalled_job_resolves_to_failed(self, book_node):
        file_id = bp._register('42', 'y.pdf', '/tmp', '')
        job_id = bp._new_job(file_id, '42', '', 'y.pdf')
        bp._update_job(job_id, status='processing')
        with bp._jobs_lock:
            bp._jobs[job_id]['updated'] = time.time() - bp.STALL_SECONDS - 1
        assert bp.get_job(job_id)['status'] == 'failed'
        assert _stored_status(file_id) == 'failed'


# ── Progress reaches the clients that already render it ───────────────

class TestProgress:

    def test_progress_carries_the_real_file_id_and_climbs_to_100(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42', 'req-7')
        assert book_node.published
        assert {uid for uid, _ in book_node.published} == {'42'}
        payloads = [p for _, p in book_node.published]
        assert {p['file_id'] for p in payloads} == {result['file_id']}
        assert {p['request_id'] for p in payloads} == {'req-7'}
        percents = [p['percentage'] for p in payloads if 'percentage' in p]
        assert percents == sorted(percents) and percents[-1] == 100
        assert [p['page_number'] for p in payloads if 'page_number' in p] == [1, 2, 3]

    def test_it_goes_out_on_the_message_bus_and_the_crossbar_topic(self, monkeypatch):
        """Through the real MessageBus: its own subscribers, and its Crossbar
        leg on the topic central's pipeline published on."""
        import types

        from core.peer_link import message_bus as mb
        bus = mb.MessageBus()
        monkeypatch.setattr(mb, '_bus', bus)
        # No native WAMP session and no PeerLink here: the Crossbar leg takes
        # the HTTP transport, which records what it would have sent.
        no_session = types.ModuleType('hartos.crossbar_server')
        no_session.wamp_session = None
        monkeypatch.setitem(sys.modules, 'hartos.crossbar_server', no_session)
        monkeypatch.setitem(sys.modules, 'core.peer_link.link_manager', None)
        local, crossbar = [], []
        bus.subscribe('book.parsing', lambda topic, data: local.append(data))
        bus.set_http_transport(lambda topic, payload: crossbar.append((topic, json.loads(payload))))

        assert bp._publish('42', {'percentage': 50, 'request_id': 'r'}) is True
        assert bp._publish('42', {'percentage': 60, 'request_id': 'r'}) is True

        assert [(d['percentage'], d['user_id']) for d in local] == [(50, '42'), (60, '42')]
        assert [topic for topic, _ in crossbar] == ['com.hertzai.bookparsing.42'] * 2
        assert [payload['percentage'] for _, payload in crossbar] == [50, 60]
        # The web client drops a repeat of a message id and, lacking one, keys
        # on request_id, which every message of one book shares: without an
        # id of its own, every page after the first would be thrown away.
        ids = [d.get('msg_id') for d in local]
        assert all(ids) and len(set(ids)) == 2

    def test_a_repeat_upload_of_a_book_already_read_goes_straight_to_100(
            self, book_node, tmp_path):
        """As central answered a repeat upload (pipeline/upload_api.py:773)."""
        pdf = make_pdf(tmp_path / 'book.pdf')
        first = bp.parse_book(pdf, '42', 'req-1')
        book_node.published.clear()
        again = bp.start_parse(pdf, '42', 'req-2')
        assert again['existing'] is True and again['file_id'] == first['file_id']
        assert [(uid, p['percentage'], p['file_id'], p['request_id'])
                for uid, p in book_node.published] == [('42', 100, first['file_id'], 'req-2')]

    def test_a_repeat_upload_of_a_book_still_being_read_adds_no_progress(
            self, book_node, tmp_path, monkeypatch):
        """Its own parse reports; a second 100% would jump the bar ahead of it."""
        monkeypatch.setattr(bp, '_worker', lambda *a, **k: None)
        pdf = make_pdf(tmp_path / 'book.pdf')
        bp.start_parse(pdf, '42')
        book_node.published.clear()
        assert bp.start_parse(pdf, '42')['existing'] is True
        assert book_node.published == []

    def test_the_sync_api_returns_what_an_agent_reads(self, book_node, tmp_path):
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        assert 'Acceleration is the rate of change of velocity.' in result['whole_text']
        assert result['chapters'] == 2
        assert result['progress_log'][-1].startswith('Done:')


# ── The units moved from Nunba's routes/upload_routes.py ──────────────
# Their Nunba tests (TestAssignChaptersToPages, TestParsePageViaVision,
# TestGenerateBookName) moved with them.

class TestAssignChapters:

    def test_an_empty_or_missing_toc_changes_nothing(self):
        pages = [{'page_number': 1, 'text': 'hello'}]
        assert bp.assign_chapters(pages, []) == [{'page_number': 1, 'text': 'hello'}]
        assert bp.assign_chapters(pages, None) == [{'page_number': 1, 'text': 'hello'}]

    def test_each_page_gets_the_range_it_falls_in(self):
        toc = [{'title': 'Introduction', 'page': 1}, {'title': 'Methods', 'page': 5},
               {'title': 'Results', 'page': 10}]
        pages = [{'page_number': n} for n in (1, 3, 5, 7, 10, 12)]
        assert [p['chapter_name'] for p in bp.assign_chapters(pages, toc)] == [
            'Introduction', 'Introduction', 'Methods', 'Methods', 'Results', 'Results']

    def test_a_name_already_on_the_page_is_kept(self):
        pages = [{'page_number': 1, 'chapter_name': 'Already Set'}]
        assert bp.assign_chapters(pages, [{'title': 'Ch1', 'page': 1}])[0]['chapter_name'] \
            == 'Already Set'

    def test_invalid_entries_are_skipped(self):
        toc = [{'title': 'Good', 'page': 1}, {'title': '', 'page': 5},
               {'title': 'Bad', 'page': 'abc'}, 'not an entry']
        pages = [{'page_number': 1}, {'page_number': 6}]
        assert [p['chapter_name'] for p in bp.assign_chapters(pages, toc)] == ['Good', 'Good']

    def test_bounds_end_a_range_without_naming_the_pages_after_it(self):
        """A chapter's last section stops at the next chapter."""
        sections = [{'title': '1.1', 'page': 1}, {'title': '1.2', 'page': 2}]
        chapters = [{'title': 'Ch 1', 'page': 1}, {'title': 'Ch 2', 'page': 4}]
        pages = [{'page_number': n} for n in (1, 2, 3, 4, 5)]
        got = bp.assign_chapters(pages, sections, field='topic_name', bounds=chapters)
        assert [p.get('topic_name') for p in got] == ['1.1', '1.2', '1.2', None, None]


class TestPageViaVision:

    @pytest.fixture(autouse=True)
    def _image(self, tmp_path):
        self.image = tmp_path / 'page_3.jpg'
        self.image.write_bytes(b'\xff\xd8\xff a page')

    def _page(self, reply):
        reader = MagicMock(return_value=reply)
        page = bp._parse_page_via_vision(3, str(self.image), [reader])
        reader.assert_called_once_with(b'\xff\xd8\xff a page', bp._PAGE_PROMPT)
        return page

    def test_a_page_one_reader_does_not_read_goes_to_the_next(self):
        """The caption server silent or down: the node's main model reads it."""
        silent = MagicMock(return_value='')
        down = MagicMock(side_effect=RuntimeError('connection refused'))
        main = MagicMock(return_value=json.dumps(
            {'page_type': 'content', 'text': 'Hello', 'elements': []}))
        page = bp._parse_page_via_vision(3, str(self.image), [silent, down, main])
        assert page['text'] == 'Hello' and not page.get('error')
        assert silent.called and down.called and main.called

    def test_a_page_no_reader_answers_is_an_error_page(self):
        page = bp._parse_page_via_vision(3, str(self.image), [MagicMock(return_value=None)])
        assert page['error']

    def test_no_answer_is_an_error_page(self):
        page = self._page(None)
        assert (page['page_number'], page['page_type']) == (3, 'unknown') and page['error']

    def test_a_json_answer_is_used(self):
        page = self._page(json.dumps({
            'page_type': 'content', 'text': 'Hello world',
            'elements': [{'type': 'paragraph', 'content': 'Hello world'}]}))
        assert (page['page_number'], page['page_type'], page['text']) == (3, 'content', 'Hello world')

    def test_a_fenced_json_answer_is_unwrapped(self):
        page = self._page('```json\n{"page_type": "cover", "text": "Title Page", "elements": []}\n```')
        assert page['page_type'] == 'cover'

    def test_a_prose_answer_becomes_the_pages_text(self):
        page = self._page('Just some text on the page')
        assert page['text'] == 'Just some text on the page'
        assert page['elements'] == [{'type': 'paragraph', 'content': 'Just some text on the page'}]

    def test_malformed_fields_are_normalised(self):
        page = self._page(json.dumps({'text': 5, 'elements': 'x',
                                      'toc_entries': [1, {'title': 'A', 'page': 2}]}))
        assert page['text'] == '' and page['elements'] == []
        assert page['toc_entries'] == [{'title': 'A', 'page': 2}]


class TestBookTitle:

    def _name(self, **post):
        with patch('core.port_registry.get_local_llm_url', return_value='http://127.0.0.1:8080/v1'), \
             patch('core.http_pool.pooled_post', **post) as pooled_post:
            return bp._generate_book_name('first page', [{'title': 'Units'}]), pooled_post

    def test_the_request_turns_thinking_off(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'choices': [{'message': {'content': ' "Physics Part One" '}}]}
        name, post = self._name(return_value=resp)
        assert name == 'Physics Part One'
        assert post.call_args.args[0] == 'http://127.0.0.1:8080/v1/chat/completions'
        assert post.call_args.kwargs['json']['chat_template_kwargs'] == {'enable_thinking': False}

    def test_a_failed_call_names_nothing(self):
        assert self._name(side_effect=RuntimeError('timeout'))[0] is None

    def test_a_non_200_names_nothing(self):
        assert self._name(return_value=MagicMock(status_code=500))[0] is None


# ── The upload helpers ────────────────────────────────────────────────


class TestUploads:

    def test_save_pdf_keeps_a_real_pdf(self, book_uploads, tmp_path):
        data = make_pdf(tmp_path / 'src.pdf').read_bytes()
        path = bp.save_pdf(data, 'My Book.pdf')
        assert path.parent == book_uploads / 'files' and path.suffix == '.pdf'
        assert path.read_bytes() == data
        assert bp.upload_url_for(path) == f'/uploads/files/{path.name}'

    @pytest.mark.parametrize('data,match', [(b'', 'empty'), (b'hello', 'not a PDF')])
    def test_save_pdf_refuses_junk(self, book_uploads, data, match):
        with pytest.raises(bp.BookParseError, match=match):
            bp.save_pdf(data, 'x.pdf')

    def test_save_pdf_refuses_an_oversized_file(self, book_uploads, monkeypatch):
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', 10)
        with pytest.raises(bp.BookParseError, match='larger than'):
            bp.save_pdf(b'%PDF-1.4 ' + b'x' * 20, 'x.pdf')

    def test_resolve_upload_url_stays_inside_the_uploads(self, book_uploads):
        target = book_uploads / 'files' / 'a.pdf'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'%PDF-1.4')
        (book_uploads.parent / 'secret.pdf').write_bytes(b'%PDF-1.4')
        assert bp.resolve_upload_url('/uploads/files/a.pdf') == target.resolve()
        assert bp.resolve_upload_url('/uploads/../secret.pdf') is None
        assert bp.resolve_upload_url('/uploads/files/missing.pdf') is None
        assert bp.resolve_upload_url('/etc/passwd') is None

    def test_a_url_naming_a_network_share_or_a_drive_touches_no_filesystem(
            self, book_uploads, monkeypatch):
        r"""The old join made '/uploads///host/share/x' the UNC path
        \\host\share\x on Windows, and touching a UNC path goes to the network."""
        touched, real_resolve = [], Path.resolve

        def spy(self, *args, **kwargs):
            touched.append(str(self))
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'resolve', spy)
        for url in ('/uploads///attacker.example/share/x.pdf',
                    '/uploads/\\\\attacker.example\\share\\x.pdf',
                    '/uploads/C:/Windows/win.ini', '/uploads/files/../../x.pdf', '/uploads/'):
            assert bp.resolve_upload_url(url) is None, url
        assert touched == []


# ── A busy library: another writer holds SQLite's lock ────────────────

def _impatient_sessions(monkeypatch, book_db, timeout=0.1):
    """Sessions on the same library file that give up on a lock after `timeout`.
    Production gives up after 3 s (busy_timeout=3000); only the scale differs.
    A test that is about the busy timeout itself passes a larger one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from integrations.social import models
    engine = create_engine(book_db.url, poolclass=NullPool,
                           connect_args={'check_same_thread': False,
                                         'timeout': timeout})
    monkeypatch.setattr(models, '_SessionLocal',
                        sessionmaker(bind=engine, expire_on_commit=False))


def _hold_write_lock(book_db, seconds):
    """Take SQLite's write lock from a second connection; let go after `seconds`."""
    blocker = sqlite3.connect(book_db.url.database, timeout=0.1,
                              check_same_thread=False, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    release = threading.Timer(seconds, lambda: (blocker.execute('ROLLBACK'), blocker.close()))
    release.start()
    return release


def _a_parse_its_lock_killed(user_id, filename):
    """A row still saying 'processing' with a FRESH updated_at, and a failed job.

    The hot window 1faa4b6a0 opened: the lock that killed the parse also
    refused _mark_failed's write, so the row looks freshly touched and the
    stall timeout cannot condemn it for 30 minutes.  Only the job knows.
    """
    from integrations.social.models import BookFile, db_session
    file_id = bp._register(user_id, filename, '/tmp', '')
    with db_session() as db:
        row = db.get(BookFile, file_id)
        row.status = 'processing'
        row.updated_at = bp._utcnow()         # fresh: only the job can call it dead
    job_id = bp._new_job(file_id, user_id, '', filename)
    bp._update_job(job_id, status='failed', error=bp._BUSY_NOT_SAVED)
    return file_id


class TestLockContention:

    def _lock_before_the_save(self, monkeypatch, book_db, seconds, holder):
        """The title call runs just before the final save: take the lock there."""
        def name_then_lock(text, toc):
            holder.append(_hold_write_lock(book_db, seconds))
            return None
        monkeypatch.setattr(bp, '_generate_book_name', name_then_lock)

    def test_a_save_that_meets_the_lock_waits_it_out(self, book_node, book_db, tmp_path,
                                                      monkeypatch):
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.3, 0.3, 0.3, 0.3))
        held = []
        self._lock_before_the_save(monkeypatch, book_db, 0.7, held)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        held[0].join()
        assert _row(result['file_id'])['status'] == 'completed'
        assert sorted(_by_page(result['file_id'])) == [1, 2, 3]

    def test_without_the_retry_the_same_lock_keeps_the_book_out(self, book_node, book_db,
                                                                tmp_path, monkeypatch):
        """The control: proves the lock in the test above really bites."""
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', ())
        held = []
        self._lock_before_the_save(monkeypatch, book_db, 0.7, held)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        held[0].join()
        assert result['stored'] is False
        assert result['store_error'] == bp._BUSY_NOT_SAVED

    def test_a_lock_mid_parse_does_not_hand_the_person_the_sql(
            self, book_node, book_db, tmp_path, monkeypatch):
        """The person is told the library was busy, not shown the statement.

        The driver's text renders [SQL: ...] and [parameters: ...], and for
        _save's executemany the parameters are the book's own page text; the
        status route hands that to anyone who can poll it.  Measured live
        2026-10-18 before this: "unexpected error: (sqlite3.OperationalError)
        database is locked [SQL: DELETE FROM page_layouts WHERE
        page_layouts.file_id = ?] [parameters: (1,)]".
        """
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', ())
        held = []
        self._lock_before_the_save(monkeypatch, book_db, 0.7, held)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')
        held[0].join()

        said = result['store_error']
        assert 'busy' in said and 'again' in said, said
        for leak in ('SQL:', 'parameters:', 'page_layouts', 'sqlite3',
                     'OperationalError', 'sqlalche.me'):
            assert leak not in said, said

    def test_the_background_parse_reports_a_lock_as_busy_on_job_and_row(
            self, book_node, book_db, tmp_path, monkeypatch):
        """The measured path: start_parse's worker, where the old answer began
        "unexpected error:" and carried the statement.  parse_book takes the
        other branch, so without this the named defect is unpinned.

        The lock is raised at the save rather than held on the file: a real
        hold also blocks _mark_failed's own write, which is the open
        row-versus-job gap and not what this pins.  Only the boundary -- the
        database error _save meets -- is faked; the worker, the classifier,
        the row and the job are the real ones.
        """
        from sqlalchemy.exc import OperationalError

        def locked(*a, **k):
            raise OperationalError('DELETE FROM page_layouts WHERE file_id = ?',
                                   {}, sqlite3.OperationalError('database is locked'))

        monkeypatch.setattr(bp, '_save', locked)
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42', 'req-lock')
        job = _wait_for_job(started['job_id'])

        assert job['status'] == 'failed'
        assert job['error'] == bp._BUSY_NOT_SAVED
        assert 'unexpected error' not in job['error']
        assert _row(started['file_id'])['error'] == bp._BUSY_NOT_SAVED

    def test_a_books_own_complaint_is_never_rewritten_as_busy(
            self, book_node, book_db, tmp_path, monkeypatch):
        """The guard for the gate in is_lock_error.

        That handler is handed every exception a parse can raise, and most
        carry no .orig, so matching the message classified
        BookParseError('the PDF could not be opened (...OS_Deadlocks.pdf)')
        as lock contention -- telling the person to re-upload a file that can
        never parse.  A lock word in a book's own complaint must not move it.
        """
        complaint = 'the PDF could not be opened (C:/u/9fa_OS_Deadlocks.pdf)'
        monkeypatch.setattr(bp, '_open',
                            lambda *a, **k: (_ for _ in ()).throw(
                                bp.BookParseError(complaint)))
        started = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42', 'req-perm')
        job = _wait_for_job(started['job_id'])

        assert job['status'] == 'failed'
        assert job['error'] == complaint
        assert job['error'] != bp._BUSY_NOT_SAVED
        assert _row(started['file_id'])['error'] == complaint

    def test_a_lock_word_outside_a_database_error_is_not_contention(self):
        """The gate's other half: the type decides, never the words.

        is_lock_error is handed every exception a whole parse can raise, and
        a plain RuntimeError carrying the driver's own sentence is the shape
        any message match reads as contention.  The measured own-goal this
        mirrors went the other way: a BookParseError naming OS_Deadlocks.pdf
        was answered "the library was busy", for a file that could never parse.
        """
        assert bp.is_lock_error(RuntimeError('database is locked')) is False
        assert bp.is_lock_error(OSError('deadlock while writing')) is False

    def test_a_save_that_fails_with_a_lock_word_is_still_not_busy(
            self, book_node, book_db, tmp_path, monkeypatch):
        """parse_book's save handler, the classifier's fourth caller.

        A RuntimeError is not the library being busy, so the person is told
        the book was not saved -- never invited to repeat an upload that will
        fail the same way again.
        """
        def not_a_database_error(*a, **k):
            raise RuntimeError('database is locked')

        monkeypatch.setattr(bp, '_save', not_a_database_error)
        result = bp.parse_book(make_pdf(tmp_path / 'book.pdf'), '42')

        assert result['stored'] is False
        assert result['store_error'] != bp._BUSY_NOT_SAVED
        assert 'could not be saved' in result['store_error']

    def test_a_retry_is_not_blocked_by_a_parse_its_lock_already_killed(
            self, book_node, book_db, tmp_path, monkeypatch):
        """The other half of the 503's advice: "try again in a moment" has to
        be TRUE for the same stored file.

        The lock that kills the parse also blocks _mark_failed, so the row
        keeps 'processing' with a fresh updated_at -- live, by its stored
        status, for the whole STALL_SECONDS.  _existing then refused to start
        another parse, and the retry we had just advised did nothing for 30
        minutes.  The job knew all along.
        """
        from sqlalchemy.exc import OperationalError
        from integrations.social.models import BookFile, db_session

        original_save = bp._save

        def locked(*a, **k):
            raise OperationalError('DELETE FROM page_layouts WHERE file_id = ?',
                                   {}, sqlite3.OperationalError('database is locked'))

        monkeypatch.setattr(bp, '_save', locked)
        pdf = make_pdf(tmp_path / 'book.pdf')
        first = bp.start_parse(pdf, '42', 'req-busy')
        assert _wait_for_job(first['job_id'])['error'] == bp._BUSY_NOT_SAVED

        # The lock blocked _mark_failed too, so the row stayed 'processing'
        # and freshly touched: the measured state, not an invented one.
        with db_session() as db:
            row = db.get(BookFile, first['file_id'])
            row.status, row.error, row.updated_at = ('processing', None,
                                                     datetime.utcnow())

        assert bp._existing('42', 'book.pdf') is None, 'a dead parse still reads as live'

        monkeypatch.setattr(bp, '_save', original_save)      # the library is free
        second = bp.start_parse(pdf, '42', 'req-retry')
        assert second.get('existing') is not True
        assert second['file_id'] != first['file_id']
        assert _wait_for_job(second['job_id'])['status'] == 'completed'

    def test_an_existing_book_never_hands_back_another_books_job(
            self, book_node, book_db, tmp_path):
        """start_parse's 'existing' branch: the rule's second call site.

        It asked for a job by bare id, so after a database reset -- where
        ids start over -- "Already in your library" could answer with a job
        belonging to a different book, and the person polled a stranger's
        failure.  Measured by the 1faa4b6a0 review.
        """
        file_id = bp._register('42', 'book.pdf', '/tmp', '')
        bp._update_row(file_id, status='completed')
        stale = bp._new_job(file_id, '42', '', 'other.pdf')   # the reset shape
        bp._update_job(stale, status='failed', error='another book died')

        answer = bp.start_parse(make_pdf(tmp_path / 'book.pdf'), '42', 'req-again')

        assert answer['existing'] is True and answer['status'] == 'completed'
        assert answer['job_id'] != stale
        assert answer['job_id'] is None

    def test_a_long_name_does_not_hide_a_dead_parse(self, book_node, book_db):
        """The job's key has to be the row's key.

        The row clips a filename to its column and the job kept the whole
        300 characters, so the two never matched: for a long name the fix
        above silently did not apply and the 30-minute wait was back.
        Measured by the 1faa4b6a0 review.
        """
        long_name = 'a' * 300 + '.pdf'
        file_id = bp._register('42', long_name, '/tmp', '')
        bp._update_row(file_id, status='processing')
        job = bp._new_job(file_id, '42', '', long_name)
        bp._update_job(job, status='failed', error=bp._BUSY_NOT_SAVED)

        assert bp._existing('42', long_name) is None

    def test_two_readers_the_column_cannot_tell_apart_are_both_refused(
            self, book_node, book_db):
        """The leak 74b39d1a4 introduced, pinned as unreachable.

        These two ids share the column's first 64 characters. 74b39d1a4 made
        list_books match the CLIPPED id, so they collapsed onto one key and
        bob's library showed alice's book -- and get_layouts keys on file_id
        with no owner check, so read_book_page would have put alice's page text
        into bob's agent prompt (measured on review).

        Clipping identity on the read was the wrong half to change. The write
        refuses an id it cannot store, so neither of these readers can own a
        row at all and the collision has nowhere to happen.
        """
        alice, bob = 'P' * 64 + 'alice', 'P' * 64 + 'bob'
        assert alice[:READER_ID_CHARS] == bob[:READER_ID_CHARS]

        for reader in (alice, bob):
            with pytest.raises(bp.BookParseError, match='cannot file a book'):
                bp._register(reader, 'book.pdf', '/tmp', '')
            assert bp.list_books(reader) == []
        assert bp.list_books() == []          # and nothing was stored for either

    def test_a_reader_id_longer_than_the_column_is_refused_not_truncated(
            self, book_node, book_db):
        """users.id is String(64), so a longer id is not a reader at all.

        Truncating it silently was the root cause: the row then held a key that
        was not the id it was given, and anything comparing the two had to
        choose between missing the row and matching someone else's.
        """
        with pytest.raises(bp.BookParseError, match='cannot file a book'):
            bp._register('u' * (READER_ID_CHARS + 1), 'mine.pdf', '/tmp', '')
        assert bp.list_books() == []

    def test_two_readers_the_column_can_hold_stay_apart(self, book_node, book_db):
        """The ordinary case the exact comparison has to keep right."""
        a, b = 'r' * 63 + 'a', 'r' * 63 + 'b'
        hers = bp._register(a, 'hers.pdf', '/tmp', '')
        his = bp._register(b, 'his.pdf', '/tmp', '')
        bp._update_row(hers, status='completed')
        bp._update_row(his, status='completed')

        assert [x['file_id'] for x in bp.list_books(a)] == [hers]
        assert [x['file_id'] for x in bp.list_books(b)] == [his]

    def test_an_over_long_id_is_never_handed_another_readers_book(
            self, book_node, book_db):
        """The same leak reached from the read side, which the refusal alone
        does not close.

        Here a real reader owns a book, and their id is exactly the first 64
        characters of a longer one. Refusing the over-long id at the write
        stops it OWNING a row, but a READ that clips would still match this
        row -- handing back another reader's file_id and status through
        start_parse's 'existing' branch, before any refusal runs, and their
        book through list_books. Found by asking what the reverted _existing
        clip would do, not by the failure.
        """
        owner = 'P' * READER_ID_CHARS
        file_id = bp._register(owner, 'book.pdf', '/tmp', '')
        bp._update_row(file_id, status='completed')
        intruder = owner + 'bob'

        assert bp._existing(intruder, 'book.pdf') is None
        assert bp.list_books(intruder) == []
        assert [b['file_id'] for b in bp.list_books(owner)] == [file_id]

    def test_another_readers_job_does_not_condemn_this_readers_book(
            self, book_node, book_db):
        """The other half of the same key, which nothing claimed.

        A job is matched by id AND filename AND reader, and the filename
        half is pinned by the test above -- but with the READER condition
        dropped from _job_for_file the whole suite stayed green (A4/M5 of
        the 1faa4b6a0 review).  Two readers can hold the same file id and
        the same name, because ids start over after a database reset, so
        without the reader one person's failed parse calls another person's
        live book dead and refuses the retry for 30 minutes.
        """
        file_id = bp._register('42', 'shared.pdf', '/tmp', '')
        bp._update_row(file_id, status='processing')
        mine = bp._new_job(file_id, '42', '', 'shared.pdf')    # still running
        bp._update_job(mine, status='processing')
        theirs = bp._new_job(file_id, '99', '', 'shared.pdf')  # newest, and not mine
        bp._update_job(theirs, status='failed', error='their parse died')

        # Newest first, so a match that ignores the reader returns theirs.
        assert bp._job_for_file(file_id, 'shared.pdf', '42') == mine
        assert bp._existing('42', 'shared.pdf') is not None

    def test_a_parse_with_no_job_of_its_own_is_still_live(self, book_node, book_db):
        """The guard for the rule above: no job does NOT mean dead.

        parse_book stores a row and never makes a job, and start_parse
        registers the row a moment before its job exists.  Reading "no job"
        as "dead" would break the duplicate-upload guard and every
        synchronous agent parse -- and would decide H-0423's restart question
        by accident.  Only the stall timeout may call those dead.
        """
        file_id = bp._register('42', 'live.pdf', '/tmp', '')
        bp._update_row(file_id, status='processing')

        assert bp._job_for_file(file_id, 'live.pdf', '42') is None
        existing = bp._existing('42', 'live.pdf')
        assert existing is not None and existing['status'] == 'processing'

        # And a failed job that merely REUSED this id is not this book's.
        # Ids start over after a database reset, so matching on the id alone
        # let a stale job condemn a fresh row -- which is how this very test
        # first failed, on a job left by the one above it.
        stale = bp._new_job(file_id, '42', '', 'another.pdf')
        bp._update_job(stale, status='failed', error='something else died')

        still = bp._existing('42', 'live.pdf')
        assert still is not None and still['status'] == 'processing'

    def test_a_read_still_answers_while_a_stall_cannot_be_recorded(self, book_node, book_db,
                                                                   monkeypatch):
        from integrations.social.models import BookFile, db_session
        file_id = bp._register('42', 'z.pdf', '/tmp', '')
        with db_session() as db:
            row = db.get(BookFile, file_id)
            row.status = 'processing'
            row.updated_at = datetime.utcnow() - timedelta(seconds=bp.STALL_SECONDS + 60)
        _impatient_sessions(monkeypatch, book_db)
        # The real ladder, not an emptied one: with @_retry_on_lock still on
        # _record_stalled this test passed only because it had removed the
        # cost it should have been measuring (that review's A1).
        release = _hold_write_lock(book_db, 0.5)
        book = bp.list_books('42')[0]                 # the write is blocked; the read is not
        assert book['status'] == 'failed' and 'stalled' in book['error']
        release.join()
        bp.list_books('42')                           # the lock is gone: now it is recorded
        with db_session(commit=False) as db:
            assert db.get(BookFile, file_id).status == 'failed'

    def test_a_library_read_under_a_lock_does_not_pay_the_write_ladder(
            self, book_node, book_db, monkeypatch):
        """Reading the library while the holder is still there.

        The answer below was always right -- a dead parse shows as failed
        whether or not the record can be written.  It is the WAITING that was
        the defect: _record_stalled was patient, so the read was too, and a
        person opening their books waited out a lock they had no stake in
        (measured 20.48 s vs 0.00 s at the parent; that review's A1).
        """
        _a_parse_its_lock_killed('42', 'hot.pdf')
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.4, 0.4, 0.4, 0.4))
        release = _hold_write_lock(book_db, 2.0)
        try:
            started = time.monotonic()
            books = bp.list_books('42')
            waited = time.monotonic() - started
        finally:
            release.join()
        assert books[0]['status'] == 'failed'
        assert waited < 1.0, f"the library read waited out the holder: {waited:.2f}s"

    def test_a_retry_checking_the_library_does_not_pay_the_write_ladder(
            self, book_node, book_db, monkeypatch):
        """start_parse's first act is _existing, a read, and it met the ladder too.

        Retrying a stored file whose parse the lock killed is precisely what
        the 503's "try again in a moment" sends a person back to do, so this
        read put the ladder in front of the one case that advice is aimed at
        -- and then _register's own, legitimate ladder followed it (measured
        40.97 s through start_parse).
        """
        _a_parse_its_lock_killed('42', 'retry.pdf')
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.4, 0.4, 0.4, 0.4))
        release = _hold_write_lock(book_db, 2.0)
        try:
            started = time.monotonic()
            # The dead parse is not live, so a retry may start: 1faa4b6a0's claim.
            assert bp._existing('42', 'retry.pdf') is None
            waited = time.monotonic() - started
        finally:
            release.join()
        assert waited < 1.0, f"the retry's library check waited out the holder: {waited:.2f}s"

    def test_a_library_read_does_not_wait_out_the_busy_timeout(
            self, book_node, book_db, monkeypatch):
        """Finding 2 of the 179a11815 review: one attempt is still a wait.

        Taking the ladder off _record_stalled cut a read under a lock from
        20.48 s to 3.38 s, not to nothing -- the single attempt still sat out
        the engine's busy_timeout.  A best-effort write on a read path should
        not wait at all, so it asks for no patience and lets the next read
        record the stall instead.  The sessions here are given a full second
        of patience, which the read must NOT spend.
        """
        _a_parse_its_lock_killed('42', 'nowait.pdf')
        _impatient_sessions(monkeypatch, book_db, timeout=1.0)
        release = _hold_write_lock(book_db, 2.0)
        try:
            started = time.monotonic()
            books = bp.list_books('42')
            waited = time.monotonic() - started
        finally:
            release.join()
        assert books[0]['status'] == 'failed'
        assert waited < 0.4, f"the read sat out the busy timeout: {waited:.2f}s"

    def test_the_failure_record_waits_out_a_lock(
            self, book_node, book_db, monkeypatch):
        """_update_row's ladder, which nothing pinned until now.

        _mark_failed swallows a write it cannot make ("could not record the
        failure"), so with the ladder off _update_row a parse killed by a lock
        loses its reason entirely and the row goes on saying 'processing' --
        and every one of the 155 tests still passed (mutant MW1 of the
        179a11815 review).  _record_stalled's docstring names _update_row as
        one of the three writes that must land; this is that claim, held.
        """
        file_id = bp._register('42', 'record.pdf', '/tmp', '')
        bp._update_row(file_id, status='processing')
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.3, 0.3, 0.3, 0.3))
        release = _hold_write_lock(book_db, 0.7)
        started = time.monotonic()
        bp._mark_failed(file_id, 'the parse died on a held lock')
        waited = time.monotonic() - started
        release.join()

        assert _stored_status(file_id) == 'failed'
        assert waited >= 0.5, f"it never waited for the lock at all: {waited:.2f}s"

    def test_the_failure_record_waits_for_the_lock_not_only_for_its_turn(
            self, book_node, book_db, monkeypatch):
        """_mark_failed must land because it WAITS, not only because it retries.

        The test above holds the lock for less time than the ladder's own
        sleeps, so the record lands there even with no patience at all -- which
        is why putting the read path's busy_timeout=0 into _update_row, a write
        that must land, survived all 157 tests (mutant MF6 of the c53ad80f9
        review).  Here the lock outlasts the whole ladder, so the write can only
        land by waiting on the busy timeout.
        """
        file_id = bp._register('42', 'patient.pdf', '/tmp', '')
        bp._update_row(file_id, status='processing')
        _impatient_sessions(monkeypatch, book_db, timeout=1.5)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.1, 0.1))   # 0.2 s of sleeps
        release = _hold_write_lock(book_db, 0.8)                    # outlasts them
        bp._mark_failed(file_id, 'the parse died on a held lock')
        release.join()

        assert _stored_status(file_id) == 'failed'

    def test_the_no_wait_is_put_back_on_a_shared_connection(
            self, book_node, book_db, monkeypatch):
        """The read path's no-wait must not outlive the record it was taken for.

        On the file engine every session gets its own NullPool connection, so a
        leak cannot be observed there -- but the in-memory engine uses
        StaticPool, where the pragma persisted into every later session
        (measured on review of c53ad80f9, where my comment claimed it "dies
        with" the connection).  A leak hands the no-wait to _register, _save
        and _update_row, the three writes that must land.  StaticPool here, so
        one connection is shared and the restore is visible.
        """
        from sqlalchemy import create_engine
        from sqlalchemy import text as sql
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from integrations.social import models
        shared = create_engine(book_db.url, poolclass=StaticPool,
                               connect_args={'check_same_thread': False, 'timeout': 5})
        monkeypatch.setattr(models, '_SessionLocal',
                            sessionmaker(bind=shared, expire_on_commit=False))

        def patience():
            with models.db_session(commit=False) as db:
                return db.execute(sql('PRAGMA busy_timeout')).scalar()

        before = patience()
        assert before == 5000                      # the patience the engine was given
        _a_parse_its_lock_killed('42', 'shared.pdf')
        assert bp.list_books('42')[0]['status'] == 'failed'     # the record really ran
        assert patience() == before

    def test_a_patience_reset_that_fails_is_said_out_loud(
            self, book_node, book_db, monkeypatch, caplog):
        """A reset that fails quietly is how the pooled leak stayed hidden.

        The reset runs through the ORM session, and a lock that refused the
        flush leaves that session needing a rollback -- so the reset raises and
        the connection keeps busy_timeout 0. At debug nobody hears it.
        609692530's message claimed this line was already a warning; it was
        not, and this test is what makes the claim true.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from integrations.social import models
        shared = create_engine(book_db.url, poolclass=StaticPool,
                               connect_args={'check_same_thread': False, 'timeout': 5})
        monkeypatch.setattr(models, '_SessionLocal',
                            sessionmaker(bind=shared, expire_on_commit=False))

        _a_parse_its_lock_killed('42', 'noisy.pdf')
        release = _hold_write_lock(book_db, 1.0)
        try:
            with caplog.at_level('WARNING'):
                assert bp.list_books('42')[0]['status'] == 'failed'   # the read answers
        finally:
            release.join()

        assert any('patience not restored' in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records]

    def test_the_row_that_starts_a_parse_waits_out_a_lock(
            self, book_node, book_db, monkeypatch):
        """_register's own ladder, which nothing pinned until now.

        Every parse begins with this insert.  When a pure helper was put
        between @_retry_on_lock and def _register, the insert lost the
        ladder -- one busy moment raised straight to the upload route, and
        the person got a 503 where they used to get their book -- while the
        whole suite stayed green (cc66abd80, found on review).
        """
        from sqlalchemy.exc import OperationalError
        import integrations.social.models as models

        real_session, calls = models.db_session, []

        def busy_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError('INSERT INTO pdf_files', {},
                                       sqlite3.OperationalError('database is locked'))
            return real_session(*args, **kwargs)

        monkeypatch.setattr(models, 'db_session', busy_once)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0, 0, 0))

        file_id = bp._register('42', 'waited.pdf', '/tmp', '')

        assert isinstance(file_id, int) and file_id > 0
        assert len(calls) == 2, 'the insert that starts a parse did not retry'
        # and the ladder is on the write, not on the helper beside it
        assert hasattr(bp._register, '__wrapped__')
        assert not hasattr(bp._book_key, '__wrapped__')

    def test_only_lock_contention_is_retried(self, monkeypatch):
        from sqlalchemy.exc import OperationalError
        calls = []

        def broken(*args, **kwargs):
            calls.append(1)
            raise OperationalError('UPDATE pdf_files', {},
                                   sqlite3.OperationalError('no such table: pdf_files'))

        monkeypatch.setattr('integrations.social.models.db_session', broken)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0, 0, 0))
        with pytest.raises(OperationalError, match='no such table'):
            bp._update_row(1, status='processing')
        assert len(calls) == 1


# ── Two parses at once: PDFium is not thread-safe ─────────────────────

    def test_the_insert_waits_out_a_lock_another_writer_really_holds(
            self, book_node, book_db, monkeypatch):
        """The same ladder, measured against SQLite rather than a stub.

        The test above counts calls to a db_session that raises, so it
        proves _register retries -- not that the ladder outlasts a lock a
        second writer is really holding.  Here one does: BEGIN IMMEDIATE
        for 0.7s while sessions give up after 0.1s, so only a working
        ladder can come back with an id.  The shape hartos-94's reviewer
        asked for, after cc66abd80 passed 138 tests with the insert
        undecorated and only a __wrapped__ assertion to catch it.
        """
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', (0.3, 0.3, 0.3, 0.3))
        release = _hold_write_lock(book_db, 0.7)
        started = time.time()
        try:
            file_id = bp._register('42', 'held.pdf', '/tmp', '')
        finally:
            release.join()
        waited = time.time() - started

        assert isinstance(file_id, int) and file_id > 0
        assert waited >= 0.6, f'it never waited for the lock ({waited:.2f}s)'
        assert bp._existing('42', 'held.pdf') is not None

    def test_a_reader_at_the_columns_limit_does_not_hide_a_dead_parse(
            self, book_node, book_db):
        """The reader half of the key, which the name half leaves unpinned.

        _book_key has to spell the reader the same way for the job as for the
        row, or a dead parse cannot be matched to its book.  Reverting only the
        user_id half of that fix passed all 138 tests (hartos-94's review of
        cc66abd80): the long-name test above cannot see it, because its reader
        is '42' either way.

        This reader used to be 70 characters, and the claim rested on BOTH
        sides clipping to 64.  Clipping identity is what let two readers share
        one stored key (the leak reworked out of 74b39d1a4), so an over-long id
        is refused at the write now and the boundary worth pinning is the
        longest id the column can actually hold.
        """
        reader = 'r' * READER_ID_CHARS
        file_id = bp._register(reader, 'book.pdf', '/tmp', '')
        bp._update_row(file_id, status='processing')
        job = bp._new_job(file_id, reader, '', 'book.pdf')
        bp._update_job(job, status='failed', error=bp._BUSY_NOT_SAVED)

        assert bp._existing(reader, 'book.pdf') is None


class TestConcurrency:

    def test_two_parses_at_once_each_read_their_own_book(self, book_node, tmp_path):
        """With the engine lock removed, this test killed the process in 4 runs
        of 4 (2026-09-13): a Windows fatal exception inside PDFium."""
        book_a = make_pdf(tmp_path / 'a.pdf', outline=None,
                          pages=[(f'Part {i}', f'Line {i} of book A') for i in range(1, 25)])
        book_b = make_pdf(tmp_path / 'b.pdf', outline=None,
                          pages=[(f'Part {i}', f'Line {i} of book B') for i in range(1, 25)])
        results, errors = {}, []

        def parse(key, pdf):
            try:
                results[key] = bp.parse_book(pdf, '42')
            except Exception as e:                      # surfaced below
                errors.append(e)

        threads = [threading.Thread(target=parse, args=(key, pdf))
                   for key, pdf in (('a', book_a), ('b', book_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        assert not errors, errors
        assert 'Line 24 of book A' in results['a']['whole_text']
        assert 'book B' not in results['a']['whole_text']
        assert 'Line 24 of book B' in results['b']['whole_text']
        assert 'book A' not in results['b']['whole_text']


# ── fetch_pdf: a PDF link the agent reads ─────────────────────────────

class TestFetch:

    def _response(self, chunks, status=200):
        response = MagicMock(status_code=status)
        response.iter_content.return_value = chunks
        return response

    def _store_fails_with(self, boom, tmp_path):
        """A download that arrives whole and then cannot be stored."""
        data = make_pdf(tmp_path / 'src.pdf').read_bytes()
        response = self._response([data])
        with patch('core.http_pool.pooled_get', return_value=response):
            with patch.object(bp, 'save_pdf', side_effect=boom):
                with pytest.raises(OSError) as raised:
                    bp.fetch_pdf('https://example.org/books/physics.pdf')
        return raised.value

    def test_a_download_that_cannot_be_stored_keeps_the_path_on_this_node(
            self, book_uploads, tmp_path, caplog):
        """The same egress _open had, one function along.

        save_pdf re-raises the OS error untouched and OSError.__str__ appends
        e.filename, so the uploads directory -- with the OS user's name in it
        -- rode out of this lane inside the exception. The class is kept, not
        swapped for a BookParseError: a caller that branches on OSError means
        "this node could not store it", and api_books answers 507 for exactly
        that rather than 400.
        """
        secret = str(book_uploads / 'files' / 'de1f_physics.pdf')
        with caplog.at_level('WARNING'):
            failure = self._store_fails_with(
                OSError(28, 'No space left on device', secret), tmp_path)

        # The EXACT text, because a substring check cannot pin this. On Windows
        # OSError.__str__ renders filename through repr, which DOUBLES the
        # backslashes, so `secret not in str(failure)` was true whether or not
        # the path leaked -- and a mutant that kept the directory and dropped
        # only the file name passed 10 of 10 (M9 of the 609692530 review).
        assert str(failure) == ('[Errno 28] this node could not store the '
                                'download (No space left on device)')
        assert ': None' not in str(failure)      # the blank-filename shape
        assert failure.errno == 28
        stored = [r for r in caplog.records if 'could not be stored' in r.getMessage()]
        assert stored and stored[0].exc_info is not None   # the operator keeps both

    def test_a_store_failure_with_no_errno_does_not_read_errno_none(
            self, book_uploads, tmp_path):
        """Passing e.errno through when it is None renders "[Errno None] ...".

        The same shape as _open's strerror: the scrub must not replace a path
        with the word None (hartos-70's catch on 74b39d1a4).
        """
        failure = self._store_fails_with(OSError('the volume went away'), tmp_path)

        assert str(failure) == ('this node could not store the download '
                                '(no reason reported)')
        assert 'Errno None' not in str(failure)

    def test_the_class_of_a_store_failure_survives_the_scrub(
            self, book_uploads, tmp_path):
        """A caller branching on the class still can.

        api_books answers 507 for an OSError out of save_pdf rather than 400,
        so the scrub must keep the subclass as well as the errno. The reviewer
        made ENOENT, EACCES and EEXIST happen for real; these are the shapes
        they came back as, and nothing pinned them (F6 of that review).
        """
        for number, strerror, expected in (
                (2, 'No such file or directory', FileNotFoundError),
                (13, 'Permission denied', PermissionError),
                (17, 'File exists', FileExistsError)):
            failure = self._store_fails_with(
                OSError(number, strerror,
                        str(book_uploads / 'files' / 'de1f_physics.pdf')), tmp_path)

            assert isinstance(failure, expected)
            assert failure.errno == number
            assert str(failure) == ('[Errno %d] this node could not store the '
                                    'download (%s)' % (number, strerror))

    def test_a_pdf_link_is_saved_into_the_uploads(self, book_uploads, tmp_path):
        data = make_pdf(tmp_path / 'src.pdf').read_bytes()
        response = self._response([data[:500], data[500:]])
        with patch('core.http_pool.pooled_get', return_value=response):
            path = bp.fetch_pdf('https://example.org/books/physics.pdf?edition=2')
        assert path.read_bytes() == data and path.name.endswith('_physics.pdf')
        response.close.assert_called_once()

    def test_a_download_stops_at_the_cap_counted_in_decoded_bytes(self, book_uploads,
                                                                  monkeypatch):
        """A raw read(N) of a compressed body can return a thousand times N."""
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', 3 * 1024 * 1024)
        pulled = []

        def endless():
            while True:
                pulled.append(1)
                yield b'%PDF-' + b'x' * (1024 * 1024)

        response = self._response(endless())
        with patch('core.http_pool.pooled_get', return_value=response):
            with pytest.raises(bp.BookParseError, match='larger than'):
                bp.fetch_pdf('https://example.org/bomb.pdf')
        assert len(pulled) <= 4
        response.close.assert_called_once()

    def test_an_http_error_is_named(self, book_uploads):
        response = self._response([], status=404)
        with patch('core.http_pool.pooled_get', return_value=response):
            with pytest.raises(bp.BookParseError, match='HTTP 404'):
                bp.fetch_pdf('https://example.org/missing.pdf')
        response.close.assert_called_once()

    def test_an_unreachable_host_is_named(self, book_uploads):
        import requests
        with patch('core.http_pool.pooled_get', side_effect=requests.ConnectionError('refused')):
            with pytest.raises(bp.BookParseError, match='could not be downloaded'):
                bp.fetch_pdf('https://example.org/x.pdf')

    def test_a_link_on_the_users_own_network_is_fetched(self, book_uploads, tmp_path):
        """A NAS or another machine on the LAN is an ordinary place for a book."""
        data = make_pdf(tmp_path / 'src.pdf').read_bytes()
        response = self._response([data])
        with patch('core.http_pool.pooled_get', return_value=response) as get:
            path = bp.fetch_pdf('http://192.168.1.20/books/physics.pdf')
        assert get.call_args.args[0] == 'http://192.168.1.20/books/physics.pdf'
        assert path.read_bytes() == data

    @pytest.mark.parametrize('url', ['http://169.254.169.254/latest/meta-data/',
                                     'file:///etc/passwd'])
    def test_a_link_that_is_never_a_book_is_refused_before_any_request(
            self, book_uploads, url):
        with patch('core.http_pool.pooled_get') as get:
            with pytest.raises(bp.BookParseError, match='refused'):
                bp.fetch_pdf(url)
        get.assert_not_called()

    def test_a_gzip_bomb_is_capped_on_what_it_inflates_to(self, book_uploads, monkeypatch):
        """The review's case, over real HTTP: 64 KB of gzip that inflates to
        64 MB. A raw read(N) counts the compressed bytes and hands on all 64 MB."""
        import http.server
        import zlib
        squeeze = zlib.compressobj(9, zlib.DEFLATED, 31)            # gzip framing
        body = squeeze.compress(b'%PDF-1.4\n')
        for _ in range(64):
            body += squeeze.compress(b'\0' * (1024 * 1024))
        body += squeeze.flush()

        class Bomb(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass                  # the client stopped reading, as it should

            def log_message(self, *args):
                pass

        for var in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy'):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(bp, 'MAX_PDF_BYTES', 1024 * 1024)
        handed, real_save = [], bp.save_pdf

        def counting_save(data, name=''):
            handed.append(len(data))
            return real_save(data, name)

        monkeypatch.setattr(bp, 'save_pdf', counting_save)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Bomb)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with pytest.raises(bp.BookParseError, match='larger than'):
                bp.fetch_pdf(f'http://127.0.0.1:{server.server_port}/book.pdf')
        finally:
            server.shutdown()
            server.server_close()
        # The cap plus at most one inflated chunk (an 8 KB read of zeros
        # inflates to about 8 MB), never the whole 64 MB.
        assert handed and handed[0] < 16 * 1024 * 1024, handed


# ── The schema the library gets on each database ──────────────────────

class TestSchema:

    def test_the_reader_id_limit_matches_its_column(self):
        """READER_ID_MAX copies pdf_files.user_id's width and could drift from
        it (that review's F2), and the tests above spell 64 out for their own
        reasons -- so something has to hold all three together."""
        from integrations.social.models import BookFile

        assert (bp.READER_ID_MAX
                == BookFile.__table__.c.user_id.type.length
                == READER_ID_CHARS)

    def _ddl(self, dialect):
        from sqlalchemy.schema import CreateTable

        from integrations.social.models import BookFile
        return str(CreateTable(BookFile.__table__).compile(dialect=dialect))

    @pytest.mark.parametrize('name', ['mysql', 'mariadb'])
    def test_a_whole_book_fits_its_text_column(self, name):
        from sqlalchemy.dialects import mysql
        from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
        dialect = mysql.dialect() if name == 'mysql' else MariaDBDialect()
        assert 'text_response LONGTEXT' in self._ddl(dialect)   # TEXT stops at 64 KB

    def test_book_ids_are_never_reused_on_sqlite(self):
        from sqlalchemy.dialects import sqlite
        assert 'AUTOINCREMENT' in self._ddl(sqlite.dialect())
