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
        """The property this has always guarded, unchanged: an empty id must
        NEVER reach the backend, or list_books answers EVERY user's books.
        The assert_not_called below is that pin and is untouched.

        What changed 2026-11-10 is only the words. This used to assert "No
        books parsed yet", which told a turn with no user to go and ask for
        a PDF upload -- the wrong problem, since there is nobody to upload
        it for. That is this module's THIRD instance of the same conflation
        (see the header: a backend outage was once reported as "no books"
        too), so the wording is now a refusal naming the real cause.
        """
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            out = _tools(user_id='')['list_books']()
        list_books.assert_not_called()
        self.assertIn('no user id', out)
        self.assertNotIn('No books parsed yet', out)

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

    def test_the_operator_gets_a_traceback_when_a_parse_cannot_start(self):
        """driver_text's docstring promises this, and this caller broke it.

        That docstring says the operator loses nothing because every caller
        logs the exception beside the sanitised answer -- and this one, the
        fourth caller it names, logged the text with no traceback at all
        (hartos-f3's review of df055008a, Minor 2).
        """
        boom = RuntimeError('the library went away')
        with self.assertLogs('integrations.learning.book_tools', level='WARNING') as got:
            answer, _ = self._parse('/uploads/files/abc_book.pdf', error=boom)
        assert 'Could not start PDF parsing' in answer
        assert any(r.exc_info is not None for r in got.records), [
            r.getMessage() for r in got.records]

    def test_a_disk_error_does_not_hand_the_agent_the_uploads_path(self):
        """The agent-facing one of the three routes driver_text was
        carrying an OSError's path out through. What this tool returns
        becomes part of a model's prompt, and that model can be a cloud
        one (H-0715).
        """
        secret = r'C:\Users\someone\uploads\files\de1f.pdf'
        out, _ = self._parse('/uploads/files/abc_book.pdf',
                             error=OSError(13, 'Permission denied', secret))

        assert 'Could not start PDF parsing' in out
        assert 'someone' not in out and 'de1f' not in out
        assert 'Permission denied' in out

    def test_a_failure_to_start_does_not_hand_the_agent_the_statement(self):
        """What this tool returns becomes part of a model's prompt.

        SQLAlchemy renders [SQL: ...] and [parameters: ...] into a
        StatementError, and _register's parameters carry the uploads
        directory. The prefix assertion above passes either way, which is
        why this went unnoticed; hartos-70's register reads an agent's tool
        output as data leaving the device (H-0715, H-1015), because the
        model answering the turn can be a cloud one.
        """
        import sqlite3
        from sqlalchemy.exc import OperationalError
        boom = OperationalError(
            'INSERT INTO pdf_files (user_id, filename, directory) VALUES (?, ?, ?)',
            {'directory': r'C:\Users\sathi\Documents\Nunba\uploads\pdf_parse'},
            sqlite3.OperationalError('disk I/O error'))

        out, _ = self._parse('/uploads/files/abc_book.pdf', error=boom)

        self.assertIn('Could not start PDF parsing', out)
        self.assertIn('disk I/O error', out)      # the person still learns what broke
        self.assertNotIn('[SQL:', out)
        self.assertNotIn('[parameters:', out)
        self.assertNotIn('Documents', out)
        self.assertNotIn('pdf_files', out)


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




# ── A named book that is not there is refused, never swapped ────────────────
OTHER_BOOK = {
    'file_id': 9,
    'user_id': '42',
    'filename': 'rain.pdf',
    'book_name': 'How Rain Happens',
    'total_pages': 1,
    'status': 'completed',
}


class ANamedBookIsNeverSwapped(unittest.TestCase):
    """_find_book used to fall through to books[0] for a non-empty name, so a
    book the user does not have was answered with the most recent book's
    chapters and pages and nothing in the reply said so.  The three callers'
    "No matching book" refusals were therefore unreachable whenever the
    library held anything.  MEASURED LIVE by hartos-16, turn
    msg_1794208521422_odljlx: list_book_chapters(book_name='The Art of
    Computer Programming') returned the chapters of 'How Rain Happens'.
    """

    def test_a_book_the_user_does_not_have_is_refused(self):
        out = _call('list_book_chapters', 'The Art of Computer Programming',
                    _books=[OTHER_BOOK] + BOOKS)
        self.assertTrue(out.startswith('Error:'), out)
        # the swap itself: no chapter of another book may appear
        self.assertNotIn('Chapter 1: Units', out)
        self.assertNotIn('"chapters"', out)

    def test_the_refusal_names_the_ask_and_the_real_names(self):
        """Owner rule 2026-11-09: a refusal says what was wrong and how to fix
        it, so the next call can be corrected."""
        out = _call('read_book_page', 1, 'The Art of Computer Programming',
                    _books=[OTHER_BOOK] + BOOKS)
        self.assertIn('read_book_page', out)
        self.assertIn('The Art of Computer Programming', out)   # what was wrong
        self.assertIn('How Rain Happens', out)                   # how to fix it
        self.assertIn('NCERT Physics Class 11', out)

    def test_read_book_chapter_refuses_in_its_own_name(self):
        out = _call('read_book_chapter', 'Chapter 1', 'Nonexistent Title')
        self.assertTrue(out.startswith('Error: the read_book_chapter'), out)

    def test_an_empty_name_still_means_the_most_recent_book(self):
        """All three signatures document "Empty = most recent book" -- the
        fallback belongs to the empty name, and only to it."""
        out = _call('list_book_chapters', '')
        self.assertIn('NCERT Physics Class 11', out)
        self.assertFalse(out.startswith('Error:'), out)

    def test_a_partial_name_and_a_filename_still_resolve(self):
        by_part = _call('list_book_chapters', 'physics')
        by_file = _call('list_book_chapters', 'ncert_physics_11.pdf')
        for out in (by_part, by_file):
            self.assertIn('Chapter 1: Units', out)

    def test_no_books_at_all_says_what_to_do_first(self):
        out = _call('list_book_chapters', 'anything', _books=[])
        self.assertTrue(out.startswith('Error:'), out)
        self.assertIn('parse_book_pdf', out)


    def test_a_turn_with_no_user_is_not_told_to_upload_a_book(self):
        """_books() returns [] both for "no user" and "no books", so the two
        looked alike at the refusal and a turn with no user id was sent to
        ask for a PDF upload -- which cannot help, there being nobody to
        upload it for."""
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            tools = dict((n, f)
                         for n, _d, f in build_book_tools({'user_id': None}))
            out = tools['list_book_chapters']('anything')
        self.assertTrue(out.startswith('Error:'), out)
        self.assertIn('no user id', out)
        self.assertNotIn('parse_book_pdf', out)

    def test_more_than_twenty_books_says_there_are_more(self):
        """Naming 20 as if they were all of them makes the agent tell the
        person their book is not there when it is just number 21."""
        many = [dict(OTHER_BOOK, file_id=i, book_name='Book %d' % i)
                for i in range(1, 26)]
        out = _call('list_book_chapters', 'not here at all', _books=many)
        self.assertTrue(out.startswith('Error:'), out)
        self.assertIn('and 5 more', out)
        self.assertIn('list_books', out)

    def test_a_refused_call_reads_the_library_once(self):
        """_find_book read it, then _no_book read it again for the names."""
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            out = _tools()['list_book_chapters'](
                'The Art of Computer Programming')
        self.assertTrue(out.startswith('Error:'), out)
        self.assertEqual(list_books.call_count, 1)


    def test_list_books_itself_does_not_tell_a_no_user_turn_to_upload(self):
        """The sibling of the _no_book fix: list_books had the same
        confusion, because _books() returns [] for a turn with no user AND
        for a user with no books, and only one of those is fixed by
        uploading a PDF."""
        list_books, get_layouts = _library()
        with patch.object(bp, 'list_books', list_books), \
             patch.object(bp, 'get_layouts', get_layouts):
            tools = dict((n, f)
                         for n, _d, f in build_book_tools({'user_id': None}))
            out = tools['list_books']()
        self.assertTrue(out.startswith('Error:'), out)
        self.assertIn('no user id', out)
        self.assertNotIn('upload a PDF', out)
        self.assertEqual(list_books.call_count, 0)

    def test_a_user_with_no_books_is_still_told_to_upload(self):
        """The other half: this one IS fixed by uploading, so the wording
        must not have moved with the fix above."""
        out = _call('list_books', _books=[])
        self.assertIn('upload a PDF', out)
        self.assertIn('parse_book_pdf', out)


if __name__ == '__main__':
    # Run as a script there is no pytest, so the autouse `_uploads` fixture
    # above never fires and `_render_images` would write page jpegs into the
    # REAL uploads directory -- the owner's own books.  Redirect it here
    # for the same reason the fixture exists, and by the same name, so the
    # two modes cannot drift.  (The class below this guard used to be
    # unreachable in script mode: the guard was above it.)
    import tempfile
    import core.platform_paths as _pp
    _pp.get_uploads_dir = lambda _d=tempfile.mkdtemp(prefix='book_nav_'): _d
    unittest.main()
