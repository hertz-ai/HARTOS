"""YouTube transcript — T3 (no auth, no browser).

Prefers `youtube_transcript_api` (pip), falls back to yt-dlp subtitles if
available, returns degraded message if neither is installed.

Domain-locked to youtube.com / youtu.be by domain_allowlist.py.
"""
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger('browser_research.scripts.youtube')

CONNECTION_MECHANISM = 'public_http'


def _extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-char video ID from any YouTube URL shape."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or '').lower()
    if host.endswith('youtu.be'):
        path = parsed.path.lstrip('/')
        return path[:11] if path else None
    if 'youtube.com' in host:
        qs = parse_qs(parsed.query)
        if 'v' in qs and qs['v']:
            return qs['v'][0][:11]
        # /shorts/<id>  /embed/<id>  /v/<id>
        match = re.match(r'^/(?:shorts|embed|v)/([\w-]{11})', parsed.path or '')
        if match:
            return match.group(1)
    return None


def transcript(url: str, language: str = 'en') -> dict:
    """Fetch a YouTube transcript.  Returns dict with text + metadata.

    On failure returns {'success': False, 'error': ...} — never raises.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return {
            'success': False,
            'connection_mechanism': CONNECTION_MECHANISM,
            'error': f'could not extract video id from url: {url!r}',
        }

    # Preferred: youtube_transcript_api (pure-python, no browser).
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            # youtube-transcript-api changed shape at 1.0: the static
            # `get_transcript` was replaced by an instance `.fetch()` returning
            # objects with a `.text` attribute rather than dicts. Support both,
            # because this code shipped against the old one and silently
            # returned "failed" on any host that had a current version.
            langs = [language, 'en']
            if hasattr(YouTubeTranscriptApi, 'get_transcript'):
                entries = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
            else:
                fetched = YouTubeTranscriptApi().fetch(video_id, languages=langs)
                entries = getattr(fetched, 'snippets', fetched)
        except Exception as exc:
            return {
                'success': False,
                'connection_mechanism': CONNECTION_MECHANISM,
                'video_id': video_id,
                'error': f'YouTubeTranscriptApi failed: {exc}',
            }

        def _seg_text(e):
            # 1.x yields snippet objects; 0.x yields plain dicts.
            return (e.get('text', '') if isinstance(e, dict)
                    else getattr(e, 'text', '') or '')

        text = ' '.join(_seg_text(e) for e in entries if _seg_text(e))
        return {
            'success': True,
            'connection_mechanism': CONNECTION_MECHANISM,
            'tool': 'youtube_transcript_api',
            'video_id': video_id,
            'language': language,
            'text': text,
            'segment_count': len(entries),
        }
    except ImportError:
        pass

    return {
        'success': False,
        'connection_mechanism': CONNECTION_MECHANISM,
        'video_id': video_id,
        'error': 'youtube_transcript_api not installed; pip install youtube-transcript-api',
    }
