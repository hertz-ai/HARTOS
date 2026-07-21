"""The transcript path is agent-facing tooling (browser_research dispatch),
so its failure mode matters: it returns {'success': False} rather than
raising, which means a broken backend looks like "no transcript available"
instead of "this is misconfigured". These cover the two ways that has
already bitten us."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.browser_research.scripts.youtube import (  # noqa: E402
    _extract_video_id, transcript)


def test_video_id_parsed_from_every_url_shape():
    for url, want in [
        ('https://www.youtube.com/watch?v=FYKHEHG02fk', 'FYKHEHG02fk'),
        ('https://youtu.be/FYKHEHG02fk', 'FYKHEHG02fk'),
        ('https://www.youtube.com/embed/FYKHEHG02fk?controls=0', 'FYKHEHG02fk'),
        ('https://www.youtube.com/shorts/FYKHEHG02fk', 'FYKHEHG02fk'),
    ]:
        assert _extract_video_id(url) == want, url
    assert _extract_video_id('https://example.com/x') is None
    assert _extract_video_id('') is None


def test_handles_both_api_generations(monkeypatch):
    """youtube-transcript-api 1.0 replaced the static get_transcript() with an
    instance .fetch() returning snippet objects instead of dicts. This code
    shipped against 0.x, so on any host with a current version every call
    returned 'YouTubeTranscriptApi failed: no attribute get_transcript' --
    reported to the agent as simply no transcript."""
    import youtube_transcript_api as mod

    class _Snippet:
        def __init__(self, text):
            self.text = text

    class _Fetched:
        snippets = [_Snippet('hello'), _Snippet('world')]

    class _NewApi:                      # 1.x shape: instance .fetch()
        def fetch(self, video_id, languages=None):
            return _Fetched()

    monkeypatch.setattr(mod, 'YouTubeTranscriptApi', _NewApi)
    r = transcript('https://youtu.be/FYKHEHG02fk')
    assert r['success'] is True
    assert r['text'] == 'hello world'
    assert r['segment_count'] == 2

    class _OldApi:                      # 0.x shape: static, dicts
        @staticmethod
        def get_transcript(video_id, languages=None):
            return [{'text': 'hello'}, {'text': 'world'}]

    monkeypatch.setattr(mod, 'YouTubeTranscriptApi', _OldApi)
    r = transcript('https://youtu.be/FYKHEHG02fk')
    assert r['success'] is True
    assert r['text'] == 'hello world'


def test_bad_url_fails_without_raising():
    r = transcript('https://example.com/not-a-video')
    assert r['success'] is False
    assert 'video id' in r['error']
