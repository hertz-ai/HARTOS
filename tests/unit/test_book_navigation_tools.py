"""Behavioural tests for the book-navigation agent surface.

These drive the REAL closures from integrations/learning/book_tools.py with the
HTTP boundary mocked, and assert on returned payloads — not on source text.
(feedback_no_grep_tests: a test must import the code, mock the boundary, call
the real function, and assert observable side-effects.)

Covers the four functional-parity behaviours the steward named:
  - page-wise navigation          test_read_page_*
  - chapter-based navigation      test_list_chapters_*, test_read_chapter_*
  - quoting a specific page image test_page_image_url_*, test_publisher_*
  - default page-wise learning    test_read_page_advertises_next_page
"""
import json
import unittest
from unittest.mock import patch, MagicMock

from integrations.learning.book_tools import (
    build_book_tools, _page_image_url, _UNREACHABLE_MSG,
)


BOOKS = [{
    'file_id': 7,
    'user_id': 42,
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
    # "not found" — the test caught the fixture, not the code.
    {'file_id': 7, 'page_number': 4, 'layout_number': 1, 'passage': 'Relative velocity.',
     'chapter_name': 'Chapter 2: Motion', 'topic_name': 'Kinematics', 'page_type': 'content'},
]


def _tools(books=None, layouts=None):
    """Build the real closures with the HTTP boundary mocked."""
    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if 'pdf_files' in url:
            resp.json.return_value = BOOKS if books is None else books
        else:
            resp.json.return_value = {'layouts': LAYOUTS if layouts is None else layouts}
        return resp

    patches = [
        patch('core.config_cache.get_book_list_api', return_value='http://x/db/pdf_files'),
        patch('core.config_cache.get_book_layouts_api', return_value='http://x/db/layouts'),
        patch('core.config_cache.get_book_parsing_api', return_value='http://x/upload/parse_pdf'),
        patch('requests.get', side_effect=fake_get),
    ]
    for p in patches:
        p.start()
    try:
        built = build_book_tools({'user_id': 42})
    finally:
        for p in patches:
            p.stop()
    return built, fake_get


def _call(name, *args, **kwargs):
    """Invoke one tool by name with the boundary mocked for the call too.

    `_images` says whether the page images exist on the backend (the HEAD
    check in _image_url).  Default True: most tests are about navigation, and
    an unmocked HEAD to http://x/... would be a network call in a unit test.
    """
    images = kwargs.pop('_images', True)
    built, fake_get = _tools(kwargs.pop('_books', None), kwargs.pop('_layouts', None))
    fn = dict((n, f) for n, _d, f in built)[name]
    head = MagicMock(return_value=MagicMock(status_code=200 if images else 404))
    with patch('core.config_cache.get_book_list_api', return_value='http://x/db/pdf_files'), \
         patch('core.config_cache.get_book_layouts_api', return_value='http://x/db/layouts'), \
         patch('requests.get', side_effect=fake_get), \
         patch('requests.head', head):
        out = fn(*args, **kwargs)
    _call.last_head = head
    return out


class ImagesOnlyWhenRendered(unittest.TestCase):
    """A text-only parse (no rasteriser in the build) writes no page images.
    A URL to a missing file renders a broken image on every client and lets
    the agent claim to show a page it cannot — so the URL is emitted only
    when the artifact is really there."""

    def test_read_page_omits_url_when_images_were_not_rendered(self):
        out = json.loads(_call('read_book_page', 3, _images=False))
        self.assertEqual(out['page_image_url'], '')
        self.assertIn('Motion in a straight line.', out['text'])   # text still served

    def test_read_chapter_omits_every_url_when_images_were_not_rendered(self):
        out = json.loads(_call('read_book_chapter', 'Units', _images=False))
        self.assertTrue(out['pages'])
        for p in out['pages']:
            self.assertEqual(p['page_image_url'], '')

    def test_existence_is_checked_once_per_book_not_per_page(self):
        _call('read_book_chapter', 'Units')          # two pages
        self.assertEqual(_call.last_head.call_count, 1)

    def test_checks_the_real_upload_path_on_the_same_backend(self):
        _call('read_book_page', 1)
        self.assertEqual(_call.last_head.call_args[0][0],
                         'http://x/uploads/pdf_parse/ncert_physics_11/page_1.jpg')


class BookToolSurface(unittest.TestCase):
    def test_all_five_tools_are_built(self):
        built, _ = _tools()
        names = {n for n, _d, _f in built}
        self.assertEqual(names, {
            'list_books', 'list_book_chapters', 'read_book_page',
            'read_book_chapter', 'parse_book_pdf',
        })

    def test_tools_are_on_the_main_leg(self):
        """A tool absent from MAIN_LEG_CORE_TOOLS is silently dropped for the
        assistant — the filter, not the append, decides visibility."""
        from core.agent_tools import MAIN_LEG_CORE_TOOLS
        for name in ('list_books', 'list_book_chapters', 'read_book_page',
                     'read_book_chapter', 'parse_book_pdf'):
            self.assertIn(name, MAIN_LEG_CORE_TOOLS, f'{name} would never reach the assistant')


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
        tells it there IS a next page — no loop hardcoded in the tool."""
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
        got = [p['page_number'] for p in out['pages']]
        self.assertEqual(got, [1, 2])

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
                             f"/uploads/pdf_parse/ncert_physics_11/page_{p['page_number']}.jpg")

    def test_read_page_carries_the_image_of_that_page(self):
        out = json.loads(_call('read_book_page', 3))
        self.assertEqual(out['page_image_url'],
                         '/uploads/pdf_parse/ncert_physics_11/page_3.jpg')

    def test_image_url_uses_filename_stem_not_book_name(self):
        """The images are written under the PDF's stem. book_name is LLM-generated
        and may contain spaces the file never had — using it yields a 404."""
        url = _page_image_url(
            {'filename': 'ncert_physics_11.pdf', 'book_name': 'NCERT Physics Class 11'}, 3)
        self.assertEqual(url, '/uploads/pdf_parse/ncert_physics_11/page_3.jpg')
        self.assertNotIn(' ', url)

    def test_no_filename_yields_no_url_rather_than_a_broken_one(self):
        self.assertEqual(_page_image_url({'book_name': 'X'}, 3), '')
        self.assertEqual(_page_image_url({'filename': 'a.pdf'}, 0), '')


class DegradedMode(unittest.TestCase):
    """"Backend down" and "no books" must never read the same.

    The previous version of this class asserted `'No books' in out` for a
    backend that refused connections — it pinned the BUG as the contract. The
    first live drive (2026-09-11) showed the consequence: a dead backend told
    the agent to ask the user to upload a PDF they had already uploaded.
    """

    def _down(self, name, *args, side_effect=OSError('connection refused')):
        with patch('core.config_cache.get_book_list_api', return_value='http://x/db/pdf_files'), \
             patch('core.config_cache.get_book_layouts_api', return_value='http://x/db/layouts'), \
             patch('core.config_cache.get_book_parsing_api', return_value='http://x/upload/parse_pdf'), \
             patch('requests.get', side_effect=side_effect):
            built = build_book_tools({'user_id': 42})
            fn = dict((n, f) for n, _d, f in built)[name]
            return fn(*args)

    def test_backend_down_is_not_reported_as_no_books(self):
        out = self._down('list_books')
        self.assertEqual(out, _UNREACHABLE_MSG)
        self.assertNotIn('No books parsed yet', out)

    def test_backend_down_never_asks_the_user_to_reupload(self):
        for name, args in (('list_books', ()), ('list_book_chapters', ()),
                           ('read_book_page', (1,)), ('read_book_chapter', ('Units',))):
            self.assertEqual(self._down(name, *args), _UNREACHABLE_MSG, name)

    def test_backend_http_500_is_unreachable_not_empty(self):
        def five_hundred(url, params=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 500
            return resp
        self.assertEqual(self._down('list_books', side_effect=five_hundred), _UNREACHABLE_MSG)

    def test_layouts_down_is_not_page_not_found(self):
        """Books answer, layouts do not: must not claim the page is missing."""
        def books_ok_layouts_down(url, params=None, timeout=None):
            if 'pdf_files' in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = BOOKS
                return resp
            raise OSError('connection refused')
        out = self._down('read_book_page', 1, side_effect=books_ok_layouts_down)
        self.assertEqual(out, _UNREACHABLE_MSG)
        self.assertNotIn('not found', out.lower())

    def test_no_books_tells_the_agent_what_to_do_next(self):
        """The contrast case: the backend ANSWERED, with nothing."""
        out = _call('list_books', _books=[])
        self.assertIn('parse_book_pdf', out)
        self.assertNotEqual(out, _UNREACHABLE_MSG)

    def test_guard_keeps_the_tool_signature_for_the_llm_schema(self):
        """register_for_llm derives the schema from the signature — the guard
        must be ON and must not collapse it to (*args, **kwargs)."""
        import inspect
        import typing
        built, _ = _tools()
        fn = dict((n, f) for n, _d, f in built)['read_book_page']
        self.assertTrue(hasattr(fn, '__wrapped__'), 'guard not applied')
        self.assertEqual(list(inspect.signature(fn).parameters), ['page_number', 'book_name'])
        self.assertIn('page_number', typing.get_type_hints(fn, include_extras=True))


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
        ok, sent = self._publish(
            full_schema=True,
            page_image_url='/uploads/pdf_parse/ncert_physics_11/page_3.jpg')
        self.assertTrue(ok)
        self.assertEqual(sent['envelope']['page_image_url'],
                         '/uploads/pdf_parse/ncert_physics_11/page_3.jpg')

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
