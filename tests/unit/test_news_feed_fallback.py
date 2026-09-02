"""Guard: fetch_news_feeds must fall back to curated DEFAULT_NEWS_FEEDS when
the model-supplied feed URLs all fail or return nothing.

Live root cause (2026-09-03): the world-news reuse agent guessed stale RSS URLs
(feeds.theguardian.com/world -> 404, www.reuters.com/rss2/ -> 401), so the tool
executed but returned ZERO articles and the agent could not curate anything.
The tool now falls back to a live-verified default feed set on an empty result.

Deterministic: FeedImporter is mocked, so no network is touched.  The fallback
BRANCH is what we guard; the default URLs themselves are verified live at
authoring time (see the constant's comment).
"""
import json

import integrations.social.feed_import as feed_import_mod
from integrations.agent_engine import news_tools


class _Item:
    def __init__(self, title):
        self.title = title
        self.link = 'https://example.test/a'
        self.author = 'a'
        self.published = None
        self.categories = ['general news']
        self.content = 'body'


class _Meta:
    title = 'Fake Feed'


def _capture_fetch_news_feeds(monkeypatch, fetch_impl):
    """Register the real tools against a mock agent, mock FeedImporter's
    fetch_feed with fetch_impl, and return the real fetch_news_feeds closure.
    """
    class _FakeImporter:
        def __init__(self, *a, **k):
            pass

        def fetch_feed(self, url):
            return fetch_impl(url)

    monkeypatch.setattr(feed_import_mod, 'FeedImporter', _FakeImporter)

    captured = {}

    class _Agent:
        def register_for_llm(self, name=None, description=None):
            def deco(fn):
                captured[name] = fn
                return fn
            return deco

        def register_for_execution(self, name=None):
            def deco(fn):
                return fn
            return deco

    news_tools.register_news_tools(_Agent(), _Agent(), '0')
    return captured['fetch_news_feeds']


def test_dead_urls_fall_back_to_default_feeds(monkeypatch):
    # Every model-supplied URL raises (dead) -> the tool must fall back.
    def _impl(url):
        if 'example-default' in url:  # our mocked "default" feeds succeed
            return _Meta(), [_Item(f'Headline from {url}')], None
        raise RuntimeError('404 Not Found')

    monkeypatch.setattr(news_tools, 'DEFAULT_NEWS_FEEDS',
                        ['https://example-default/1', 'https://example-default/2'])
    fetch = _capture_fetch_news_feeds(monkeypatch, _impl)

    out = json.loads(fetch('https://feeds.theguardian.com/world,https://www.reuters.com/rss2/'))
    assert out['total'] > 0, (
        'the tool returned no articles even though the fallback feeds are '
        'reachable — the agent cannot curate anything (the live bug)')
    assert out['used_default_feeds'] is True
    assert any('example-default' in it['source'] or 'Headline' in it['title']
               for it in out['items'])


def test_working_urls_do_not_trigger_fallback(monkeypatch):
    # The model's URLs work -> strict no-op, defaults are NOT fetched.
    calls = []

    def _impl(url):
        calls.append(url)
        return _Meta(), [_Item(f'Good {url}')], None

    monkeypatch.setattr(news_tools, 'DEFAULT_NEWS_FEEDS',
                        ['https://example-default/should-not-be-used'])
    fetch = _capture_fetch_news_feeds(monkeypatch, _impl)

    out = json.loads(fetch('https://good.example/rss'))
    assert out['total'] > 0
    assert out['used_default_feeds'] is False
    assert all('example-default' not in u for u in calls), (
        'default feeds were fetched even though the supplied feed worked')


def test_news_tools_registered_for_llm_on_both_agents():
    """Both the Helper AND the Assistant must carry the news tools' LLM schema.

    Live root cause (2026-09-03): the Assistant only had the tools in its
    execution map, not its LLM schema, so when speaker-selection let the
    Assistant propose a news tool, llama.cpp's --jinja grammar had no schema to
    constrain it and the model free-formed the Qwen3 <tool_call> as plain text,
    which autogen never executes.  Guard that the assistant.register_for_llm
    line is not silently dropped.
    """
    llm = {'helper': set(), 'assistant': set()}
    execu = {'helper': set(), 'assistant': set()}

    def _mk(bucket_llm, bucket_exec):
        class _Agent:
            def register_for_llm(self, name=None, description=None):
                def deco(fn):
                    bucket_llm.add(name); return fn
                return deco

            def register_for_execution(self, name=None):
                def deco(fn):
                    bucket_exec.add(name); return fn
                return deco
        return _Agent()

    helper = _mk(llm['helper'], execu['helper'])
    assistant = _mk(llm['assistant'], execu['assistant'])
    news_tools.register_news_tools(helper, assistant, '0')

    assert 'fetch_news_feeds' in llm['helper']
    assert 'fetch_news_feeds' in llm['assistant'], (
        "Assistant lost its news-tool LLM schema — it will emit <tool_call> as "
        "unexecuted text again (the 2026-09-03 live bug)")
    # Execution still lives on the Assistant (the executor side is unchanged).
    assert 'fetch_news_feeds' in execu['assistant']


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
