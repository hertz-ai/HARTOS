"""A crawl that fails tells the model what to do next, not only that it failed.

MEASURED LIVE on the desktop 4B, 2026-11-16 14:39 IST (build 75, agent Quick
Research Brief 1794818852965, REUSE turn): with the crawler now attached
(f72eda5a0), the model called crawl4ai_crawl twice with page paths it made up
on the right site,
    https://www.allcaretherapies.com/child-needs-speech-therapy        -> 404
    https://www.allcaretherapies.com/blog/signs-your-child-needs-...   -> 404
and the tool answered only "FAILED: <url> / Error: 404 Client Error".  The
step was given up and the person was told the crawl "encountered an error".
Owner rule (2026-11-09): every tool refusal says what was wrong and what to do
instead.  A failed crawl now says: use a link exactly as the search returned
it, never a guessed or edited path, and say plainly when no link can be read.

    python -m pytest tests/unit/test_a_failed_crawl_tells_the_model_what_to_do.py -q --noconftest
"""
import integrations.web_crawler as wc


def test_a_404_says_to_use_the_search_results_link_exactly(monkeypatch):
    monkeypatch.setattr(wc, 'crawl_url', lambda url, timeout=30: {
        'success': False, 'url': url, 'markdown': '', 'word_count': 0,
        'error': '404 Client Error: Not Found', 'progress': ''})
    out = wc.crawl_url_for_agent('https://www.example.org/made-up-path')
    assert 'FAILED: https://www.example.org/made-up-path' in out
    assert 'exactly as the search' in out, out
    assert 'guess' in out, out


def test_a_page_that_was_read_carries_no_such_hint(monkeypatch):
    monkeypatch.setattr(wc, 'crawl_url', lambda url, timeout=30: {
        'success': True, 'url': url, 'markdown': 'Three facts.', 'word_count': 2,
        'error': None, 'progress': ''})
    out = wc.crawl_url_for_agent('https://www.example.org/page')
    assert 'Three facts.' in out and 'exactly as the search' not in out
