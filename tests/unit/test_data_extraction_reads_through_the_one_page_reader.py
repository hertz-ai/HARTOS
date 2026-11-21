"""data_extraction_from_url reads a website through the one page reader.

WHAT WAS WRONG (audit row H19, parallel path)
  The tool carried its own fetch and cut beside integrations.web_crawler.  Its
  first branch asked service_tool_registry.get_tool('Crawl4AI'); the registry
  (ServiceToolRegistry) has no get_tool, the AttributeError was swallowed, and
  every page was answered by the second branch: the first 5000 raw characters
  of the HTML with the tags stripped, which on a real page is mostly its
  <head>, scripts and menus.

THE RULE
  - a website is read by web_crawler.crawl_url (crawl4ai when it runs, else the
    reader's own plain read) and fitted by web_crawler._fit_for_agent;
  - CARRIED OVER, unchanged for every caller: the tool's name, its two
    arguments, its description, the 'Extracted from <url>:' answer, the PDF
    answer, the 'URL extraction failed:' answer, and the direct read (tags
    stripped, 4000 characters) whenever the reader could not read the page.

    python -m pytest tests/unit/test_data_extraction_reads_through_the_one_page_reader.py -q
"""
import inspect
from types import SimpleNamespace
from unittest import mock

import pytest

URL = 'https://example.org/speech-apps'
PAGE = ('<html><head><title>Speech apps</title><script>var a = 1;</script>'
        '</head><body><p>Games keep practice short.</p></body></html>')


@pytest.fixture
def tool():
    from core.agent_tools import build_core_tool_closures
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='1_p1',
               request_id_list={'1_p1': 'r1'}, memory_graph=mock.Mock(),
               simplemem_store=None, helper_fun=mock.Mock(),
               send_message_to_user1=mock.Mock(), retrieve_json=lambda v: v)
    return {n: (d, f) for n, d, f in build_core_tool_closures(ctx)}[
        'data_extraction_from_url']


@pytest.fixture
def wire(monkeypatch):
    """The reader and the direct read, both faked; what each was asked."""
    import requests
    from integrations import web_crawler
    seen = SimpleNamespace(crawled=[], fetched=[], body=PAGE, raises=None,
                           page={'success': False, 'error': 'HTTP 404'})

    def _crawl(url, timeout=30):
        seen.crawled.append((url, timeout))
        return dict(seen.page, url=url)

    def _get(url, **kw):
        seen.fetched.append((url, kw))
        if seen.raises:
            raise seen.raises
        return SimpleNamespace(text=seen.body, content=seen.body.encode(),
                               status_code=200)
    monkeypatch.setattr(web_crawler, 'crawl_url', _crawl)
    monkeypatch.setattr(requests, 'get', _get)
    return seen


# -- carried over: these pass before the fold and after it --

def test_the_tool_keeps_its_name_arguments_and_description(tool):
    desc, func = tool
    assert list(inspect.signature(func).parameters) == ['url', 'url_type']
    assert inspect.signature(func).parameters['url_type'].default == 'website'
    assert desc == (
        "Extract content from a URL (PDF or website). Input: URL and type "
        "('pdf' or 'website'). Uses Crawl4AI for rich extraction with "
        "fallback to direct HTTP fetch.")


def test_a_page_the_reader_cannot_read_gets_the_direct_read(tool, wire):
    out = tool[1](URL)
    assert out == (f'Extracted from {URL}:\n'
                   'Speech apps var a = 1; Games keep practice short.')
    assert wire.fetched == [(URL, {'timeout': 30,
                                   'headers': {'User-Agent': 'Mozilla/5.0'}})]


def test_the_direct_read_keeps_its_cut(tool, wire):
    wire.body = '<p>' + 'word ' * 3000 + '</p>'
    out = tool[1](URL)
    head = f'Extracted from {URL}:\n'
    assert out.startswith(head)
    assert len(out) - len(head) == 4000
    assert out.endswith('word ')


def test_a_pdf_is_downloaded_and_counted_as_before(tool, wire):
    wire.body = '%PDF-1.7 ' + 'x' * 91
    out = tool[1](URL + '.pdf', 'pdf')
    assert out == 'PDF downloaded (100 bytes). Use a PDF parser for full extraction.'
    assert wire.crawled == [], 'a PDF is not sent to the page reader'


def test_a_read_that_raises_is_answered_as_a_failure(tool, wire):
    wire.raises = ConnectionError('no route to host')
    assert tool[1](URL) == 'URL extraction failed: no route to host'


# -- the fold --

def test_a_website_is_read_by_the_page_reader(tool, wire):
    wire.page = {'success': True, 'word_count': 6,
                 'markdown': '# Speech apps\n\nGames keep practice short.'}
    out = tool[1](URL)
    assert out == f'Extracted from {URL}:\n# Speech apps\n\nGames keep practice short.'
    assert wire.crawled == [(URL, 30)]
    assert wire.fetched == [], 'one read: the page reader already has the page'


def test_a_long_page_is_fitted_by_the_readers_own_rule(tool, wire):
    from integrations import web_crawler
    long_page = ('![chart](https://example.org/a.png) A sentence about speech '
                 'apps. ') * 400
    wire.page = {'success': True, 'markdown': long_page, 'word_count': 3000}
    out = tool[1](URL)
    fitted = web_crawler._fit_for_agent(long_page, 5000, '\n[Content truncated]')
    assert out == f'Extracted from {URL}:\n{fitted}'
    assert len(fitted) < len(long_page)
    assert fitted.endswith('[Content truncated]')


def test_a_reader_answer_with_no_text_gets_the_direct_read(tool, wire):
    wire.page = {'success': True, 'markdown': '', 'word_count': 0}
    out = tool[1](URL)
    assert out.endswith('Games keep practice short.')
    assert len(wire.fetched) == 1


def test_a_failed_read_that_still_carries_text_gets_the_direct_read(tool, wire):
    wire.page = {'success': False, 'markdown': 'Enable JavaScript to continue.',
                 'error': 'timeout'}
    out = tool[1](URL)
    assert 'Enable JavaScript' not in out
    assert out.endswith('Games keep practice short.')


def test_the_tool_asks_no_registry_for_a_crawler(tool, wire, monkeypatch):
    """The old first branch: service_tool_registry.get_tool('Crawl4AI')."""
    import integrations.service_tools as st
    asked = []
    monkeypatch.setattr(st.service_tool_registry, 'get_tool',
                        lambda name: asked.append(name), raising=False)
    tool[1](URL)
    assert asked == []
