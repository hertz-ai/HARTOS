"""Behavioural tests for the book-navigation agent surface.

These drive the REAL closures from integrations/learning/book_tools.py with the
library boundary (book_pipeline's reads) stubbed and page images written to a
real per-test uploads directory, and assert on the returned payloads -- not on
source text. (feedback_no_grep_tests: a test must import the code, stub the
boundary, call the real function, and assert observable side-effects.)

Covers the four functional-parity behaviours the steward named:
  - page-wise navigation          PageWiseNavigation
  - chapter-based navigation      ChapterNavigation
  - quoting a specific page image PageImageQuoting, ImagesOnlyWhenRendered,
                                  PublisherCarriesTheImage
  - default page-wise learning    test_read_page_advertises_next_page
"""
import json
import unittest
from unittest.mock import MagicMock, patch

import pytest

from integrations.learning import book_pipeline as bp
from integrations.learning.book_tools import (
    build_book_tools, _page_image_url, _UNREACHABLE_MSG,
)

BOOKS = [{
    'file_id': 7,
    'user_id': '42',
    'filename': 'ncert_physics_11.pdf',
    'book_name': 'NCERT Physics Class 11',
    'total_pages': 4,
    'status': 'completed',
}]

LAYOUTS = [
    {'file_id': 7, 'page_number': 1, 'layout_number': 1, 'passage': 'Units and Measurement intro.',
     'chapter_name': 'Chapter 1: Units', 'topic_name': 'Units', 'page_type': 'content'},
    {'file_id': 7, 'page_number': 1, 'layout_number': 2, 'passage': 'SI base quantities.',
     'chapter_name': 'Chapter 1: Units', 'topic_name': 'Units', 'page_type': 'content'},
    {'file_id': 7, 'page_number': 2, 'layout_number': 1, 'passage': 'Dimensional analysis.',
     'chapter_name': 'Chapter 1: Units', 'topic_name': 'Dimensions', 'page_type': 'content'},
    {'file_id': 7, 'page_number': 3, 'layout_number': 1, 'passage': 'Motion in a straight line.',
     'chapter_name': 'Chapter 2: Motion', 'topic_name': 'Kinematics', 'page_type': 'content'},
    # Page 4 exists because the book record says total_pages=4. Without it the
    # fixture contradicted itself and read_book_page(4) correctly answered
    # "not found" -- the test caught the fixture, not the code.
    {'file_id': 7, 'page_number': 4, 'layout_number': 1, 'passage': 'Relative velocity.',
     'chapter_name': 'Chapter 2: Motion', 'topic_name': 'Kinematics', 'page_type': 'content'},
]


@pytest.fixture(autouse=True)
def _uploads(tmp_path, monkeypatch):
    """A fresh uploads dir per test: book_pipeline keeps the page images there."""
    monkeypatch.setattr('core.platform_paths.get_uploads_dir', lambda: str(tmp_path))


def _library(books=None, layouts=None):
    """Stubs for book_pipeline's two reads, returning copies like the DB does."""
    books = BOOKS if books is None else books
    layouts = LAYOUTS if layouts is None else layouts
    list_books = MagicMock(side_effect=lambda user_id=None, limit=100: [dict(b) for b in books])
    get_layouts = MagicMock(side_effect=lambda file_id, page_number=None: [
        dict(r) for r in layouts if r['file_id'] == file_id])
    return list_books, get_layouts


def _render_images(pages):
    """Write book 7's page images for `pages`, and remove any other page's."""
    for page in range(1, BOOKS[0]['total_pages'] + 1):
        image = bp.page_image_path(7, page)
        if page in pages:
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b'\xff\xd8\xff jpeg')
        elif image.exists():
            image.unlink()


def _tools(user_id=42):
    return dict((n, f) for n, _d, f in build_book_tools({'user_id': user_id}))


def _call(name, *args, _books=None, _layouts=None, _images=(1, 2, 3, 4), **kwargs):
    """Invoke one tool with the library stubbed and only `_images` rendered."""
    _render_images(_images)
    list_books, get_layouts = _library(_books, _layouts)
    with patch.object(bp, 'list_books', list_books), \
         patch.object(bp, 'get_layouts', get_layouts):
        return _tools()[name](*args, **kwargs)


class BookToolSurface(unittest.TestCase):
    def test_all_five_tools_are_built(self):
        names = {n for n, _d, _f in build_book_tools({'user_id': 42})}
        self.assertEqual(names, {
            'list_books', 'list_book_chapters', 'read_book_page',
            'read_book_chapter', 'parse_book_pdf',
        })

    def test_tools_are_on_the_main_leg(self):
        """A tool absent from MAIN_LEG_CORE_TOOLS is silently dropped for the
        assistant -- the filter, not the append, decides visibility."""
        from core.agent_tools import MAIN_LEG_CORE_TOOLS
        for name in ('list_books', 'list_book_chapters', 'read_book_page',
                     'read_book_chapter', 'parse_book_pdf'):
            self.assertIn(name, MAIN_LEG_CORE_TOOLS, f'{name} would never reach the assistant')

    def test_a_turn_with_no_user_sees_no_books(self):
        """list_books answers EVERY user's books for an empty id."""
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            out = _tools(user_id='')['list_books']()
        list_books.assert_not_called()
        self.assertIn('No books parsed yet', out)

    def test_the_library_is_read_for_the_turns_user(self):
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            _tools(user_id=99)['list_books']()
        self.assertEqual(list_books.call_args.args, (99,))


class PageWiseNavigation(unittest.TestCase):
    def test_read_page_returns_text_of_that_page_only(self):
        out = json.loads(_call('read_book_page', 1))
        self.assertEqual(out['page_number'], 1)
        self.assertIn('Units and Measurement intro.', out['text'])
        self.assertIn('SI base quantities.', out['text'])
        self.assertNotIn('Dimensional analysis.', out['text'])  # that is page 2

    def test_read_page_joins_layouts_in_order(self):
        out = json.loads(_call('read_book_page', 1))
        self.assertLess(out['text'].index('Units and Measurement'),
                        out['text'].index('SI base quantities'))

    def test_read_page_advertises_next_page(self):
        """Default page-wise learning: the model continues because the tool
        tells it there IS a next page -- no loop hardcoded in the tool."""
        out = json.loads(_call('read_book_page', 1))
        self.assertTrue(out['has_next'])
        self.assertEqual(out['next_page'], 2)

    def test_last_page_has_no_next(self):
        out = json.loads(_call('read_book_page', 4))
        self.assertFalse(out['has_next'])
        self.assertIsNone(out['next_page'])

    def test_missing_page_reports_available_range(self):
        out = _call('read_book_page', 99)
        self.assertIn('not found', out.lower())
        self.assertNotIn('Traceback', out)


class ChapterNavigation(unittest.TestCase):
    def test_chapters_have_page_ranges_in_order(self):
        out = json.loads(_call('list_book_chapters'))
        chapters = out['chapters']
        self.assertEqual([c['chapter'] for c in chapters],
                         ['Chapter 1: Units', 'Chapter 2: Motion'])
        self.assertEqual(chapters[0]['first_page'], 1)
        self.assertEqual(chapters[0]['last_page'], 2)
        self.assertEqual(chapters[1]['first_page'], 3)

    def test_read_chapter_returns_only_its_pages(self):
        out = json.loads(_call('read_book_chapter', 'Units'))
        self.assertEqual([p['page_number'] for p in out['pages']], [1, 2])

    def test_unknown_chapter_lists_the_real_ones(self):
        out = _call('read_book_chapter', 'Thermodynamics')
        self.assertIn('not found', out.lower())
        self.assertIn('Chapter 1: Units', out)

    def test_book_with_no_chapter_names_degrades_to_page_navigation(self):
        flat = [dict(r, chapter_name=None) for r in LAYOUTS]
        out = json.loads(_call('list_book_chapters', _layouts=flat))
        self.assertEqual(out['chapters'], [])
        self.assertIn('page', out['note'].lower())


class PageImageQuoting(unittest.TestCase):
    def test_every_chapter_page_carries_its_own_image(self):
        out = json.loads(_call('read_book_chapter', 'Units'))
        for p in out['pages']:
            self.assertEqual(p['page_image_url'],
                             f"/uploads/pdf_parse/7/page_{p['page_number']}.jpg")

    def test_read_page_carries_the_image_of_that_page(self):
        out = json.loads(_call('read_book_page', 3))
        self.assertEqual(out['page_image_url'], '/uploads/pdf_parse/7/page_3.jpg')

    def test_the_url_is_keyed_by_the_book_not_its_name(self):
        """book_name is LLM-generated and two uploads can share a filename;
        the book's own id is unique."""
        _render_images((3,))
        url = _page_image_url({'file_id': 7, 'book_name': 'NCERT Physics Class 11',
                               'filename': 'a b.pdf'}, 3)
        self.assertEqual(url, '/uploads/pdf_parse/7/page_3.jpg')

    def test_no_book_id_or_page_yields_no_url_rather_than_a_broken_one(self):
        self.assertEqual(_page_image_url({'book_name': 'X'}, 3), '')
        self.assertEqual(_page_image_url({'file_id': 7}, 0), '')


class ImagesOnlyWhenRendered(unittest.TestCase):
    """A page whose image was never written must not get a URL: a missing file
    renders a broken image on every client and lets the agent claim to show a
    page it cannot."""

    def test_read_page_omits_url_when_the_image_was_not_rendered(self):
        out = json.loads(_call('read_book_page', 3, _images=()))
        self.assertEqual(out['page_image_url'], '')
        self.assertIn('Motion in a straight line.', out['text'])   # text still served

    def test_read_chapter_omits_every_missing_image(self):
        out = json.loads(_call('read_book_chapter', 'Units', _images=()))
        self.assertTrue(out['pages'])
        for p in out['pages']:
            self.assertEqual(p['page_image_url'], '')

    def test_only_pages_that_have_an_image_get_a_url(self):
        out = json.loads(_call('read_book_chapter', 'Units', _images=(1,)))
        urls = {p['page_number']: p['page_image_url'] for p in out['pages']}
        self.assertEqual(urls, {1: '/uploads/pdf_parse/7/page_1.jpg', 2: ''})


class DegradedMode(unittest.TestCase):
    """"The library could not be read" and "no books" must never read the same.

    An earlier version asserted `'No books' in out` for a dead backend -- it
    pinned the BUG as the contract. The first live drive (2026-09-11) showed
    the consequence: the agent asked the user to upload a PDF they had already
    uploaded.
    """

    def _down(self, name, *args, books_error=OSError('database is locked'), layouts_error=None):
        list_books, get_layouts = _library()
        if books_error is not None:
            list_books = MagicMock(side_effect=books_error)
        if layouts_error is not None:
            get_layouts = MagicMock(side_effect=layouts_error)
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            return _tools()[name](*args)

    def test_library_down_is_not_reported_as_no_books(self):
        out = self._down('list_books')
        self.assertEqual(out, _UNREACHABLE_MSG)
        self.assertNotIn('No books parsed yet', out)

    def test_library_down_never_asks_the_user_to_reupload(self):
        for name, args in (('list_books', ()), ('list_book_chapters', ()),
                           ('read_book_page', (1,)), ('read_book_chapter', ('Units',))):
            self.assertEqual(self._down(name, *args), _UNREACHABLE_MSG, name)

    def test_pages_unreadable_is_not_page_not_found(self):
        """Books read fine, pages do not: must not claim the page is missing."""
        out = self._down('read_book_page', 1, books_error=None,
                         layouts_error=OSError('disk I/O error'))
        self.assertEqual(out, _UNREACHABLE_MSG)
        self.assertNotIn('not found', out.lower())

    def test_no_books_tells_the_agent_what_to_do_next(self):
        """The contrast case: the library WAS read, and is empty."""
        out = _call('list_books', _books=[])
        self.assertIn('parse_book_pdf', out)
        self.assertNotEqual(out, _UNREACHABLE_MSG)

    def test_a_failed_book_says_why(self):
        failed = [dict(BOOKS[0], status='failed', error='this PDF is password-protected')]
        out = json.loads(_call('list_books', _books=failed))
        self.assertEqual(out['books'][0]['error'], 'this PDF is password-protected')

    def test_guard_keeps_the_tool_signature_for_the_llm_schema(self):
        """register_for_llm derives the schema from the signature -- the guard
        must be ON and must not collapse it to (*args, **kwargs)."""
        import inspect
        import typing
        fn = _tools()['read_book_page']
        self.assertTrue(hasattr(fn, '__wrapped__'), 'guard not applied')
        self.assertEqual(list(inspect.signature(fn).parameters), ['page_number', 'book_name'])
        self.assertIn('page_number', typing.get_type_hints(fn, include_extras=True))


class ParseBookPdf(unittest.TestCase):
    """parse_book_pdf starts the ONE pipeline, in-process, on this node."""

    def _parse(self, file_url, started=None, error=None):
        (bp.files_dir() / 'abc_book.pdf').write_bytes(b'%PDF-1.4 x')
        start = MagicMock(return_value=started, side_effect=error)
        with patch.object(bp, 'start_parse', start):
            return _tools()['parse_book_pdf'](file_url), start

    def test_it_starts_the_pipeline_for_an_uploaded_file(self):
        out, start = self._parse('/uploads/files/abc_book.pdf',
                                 started={'job_id': 'j1', 'file_id': 3, 'status': 'queued'})
        body = json.loads(out)
        self.assertEqual((body['status'], body['job_id'], body['file_id']), ('parsing', 'j1', 3))
        path, user_id = start.call_args.args
        self.assertEqual((path.name, user_id), ('abc_book.pdf', 42))

    def test_an_already_parsed_upload_is_not_parsed_again(self):
        out, _ = self._parse('/uploads/files/abc_book.pdf',
                             started={'job_id': None, 'file_id': 3, 'status': 'completed',
                                      'existing': True})
        self.assertEqual(json.loads(out)['status'], 'completed')

    def test_a_url_outside_the_uploads_is_refused(self):
        out, start = self._parse('/etc/passwd', started={})
        self.assertIn('No uploaded PDF', out)
        start.assert_not_called()

    def test_a_failure_to_start_is_reported_not_raised(self):
        out, _ = self._parse('/uploads/files/abc_book.pdf', error=RuntimeError('db locked'))
        self.assertIn('Could not start PDF parsing', out)


class PublisherCarriesTheImage(unittest.TestCase):
    """The shared publisher change: page_image_url was an unconditional ''."""

    def _publish(self, **kw):
        sent = {}

        def fake_publish_async(topic, payload):
            sent['topic'] = topic
            sent['envelope'] = json.loads(payload)

        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=fake_publish_async):
            from core.peer_link.crossbar_publish import publish_thinking_trace
            ok = publish_thinking_trace(text='t', user_id='42', **kw)
        return ok, sent

    def test_default_is_byte_identical_empty(self):
        """Every existing caller must keep producing ''."""
        ok, sent = self._publish(full_schema=True)
        self.assertTrue(ok)
        self.assertEqual(sent['envelope']['page_image_url'], '')

    def test_page_image_url_reaches_the_wire(self):
        ok, sent = self._publish(full_schema=True,
                                 page_image_url='/uploads/pdf_parse/7/page_3.jpg')
        self.assertTrue(ok)
        self.assertEqual(sent['envelope']['page_image_url'], '/uploads/pdf_parse/7/page_3.jpg')

    def test_published_on_the_users_chat_topic(self):
        _ok, sent = self._publish(full_schema=True, page_image_url='/uploads/x/page_1.jpg')
        self.assertEqual(sent['topic'], 'com.hertzai.hevolve.chat.42')

    def test_non_full_schema_still_omits_the_field(self):
        """The slim envelope has never carried it; adding it would be a new
        contract for clients that do not expect it."""
        _ok, sent = self._publish(full_schema=False, page_image_url='/uploads/x/page_1.jpg')
        self.assertNotIn('page_image_url', sent['envelope'])


if __name__ == '__main__':
    unittest.main()
