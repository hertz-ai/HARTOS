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


def _row(file_id):
    return bp.get_book(file_id)


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
        assert _row(file_id)['status'] == 'failed'          # recorded, not just shown

    def test_a_stalled_job_resolves_to_failed(self, book_node):
        file_id = bp._register('42', 'y.pdf', '/tmp', '')
        job_id = bp._new_job(file_id, '42', '', 'y.pdf')
        bp._update_job(job_id, status='processing')
        with bp._jobs_lock:
            bp._jobs[job_id]['updated'] = time.time() - bp.STALL_SECONDS - 1
        assert bp.get_job(job_id)['status'] == 'failed'
        assert _row(file_id)['status'] == 'failed'


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

def _impatient_sessions(monkeypatch, book_db):
    """Sessions on the same library file that give up on a lock after 0.1 s.
    Production gives up after 3 s (busy_timeout=3000); only the scale differs."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from integrations.social import models
    engine = create_engine(book_db.url, poolclass=NullPool,
                           connect_args={'check_same_thread': False, 'timeout': 0.1})
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
        assert result['stored'] is False and 'database is locked' in result['store_error']

    def test_a_read_still_answers_while_a_stall_cannot_be_recorded(self, book_node, book_db,
                                                                   monkeypatch):
        from integrations.social.models import BookFile, db_session
        file_id = bp._register('42', 'z.pdf', '/tmp', '')
        with db_session() as db:
            row = db.get(BookFile, file_id)
            row.status = 'processing'
            row.updated_at = datetime.utcnow() - timedelta(seconds=bp.STALL_SECONDS + 60)
        _impatient_sessions(monkeypatch, book_db)
        monkeypatch.setattr(bp, '_LOCK_RETRY_DELAYS', ())
        release = _hold_write_lock(book_db, 0.5)
        book = bp.list_books('42')[0]                 # the write is blocked; the read is not
        assert book['status'] == 'failed' and 'stalled' in book['error']
        release.join()
        bp.list_books('42')                           # the lock is gone: now it is recorded
        with db_session(commit=False) as db:
            assert db.get(BookFile, file_id).status == 'failed'

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
