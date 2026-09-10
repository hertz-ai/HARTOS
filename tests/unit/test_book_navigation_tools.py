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

from integrations.learning.book_tools import build_book_tools, _page_image_url


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
    """Invoke one tool by name with the boundary mocked for the call too."""
    built, fake_get = _tools(kwargs.pop('_books', None), kwargs.pop('_layouts', None))
    fn = dict((n, f) for n, _d, f in built)[name]
    with patch('core.config_cache.get_book_list_api', return_value='http://x/db/pdf_files'), \
         patch('core.config_cache.get_book_layouts_api', return_value='http://x/db/layouts'), \
         patch('requests.get', side_effect=fake_get):
        return fn(*args, **kwargs)


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
    def test_backend_down_returns_a_message_not_an_exception(self):
        with patch('core.config_cache.get_book_list_api', return_value='http://x/db/pdf_files'), \
             patch('core.config_cache.get_book_layouts_api', return_value='http://x/db/layouts'), \
             patch('core.config_cache.get_book_parsing_api', return_value='http://x/upload/parse_pdf'), \
             patch('requests.get', side_effect=OSError('connection refused')):
            built = build_book_tools({'user_id': 42})
            fn = dict((n, f) for n, _d, f in built)['list_books']
            out = fn()
        self.assertIn('No books', out)

    def test_no_books_tells_the_agent_what_to_do_next(self):
        out = _call('list_books', _books=[])
        self.assertIn('parse_book_pdf', out)


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
