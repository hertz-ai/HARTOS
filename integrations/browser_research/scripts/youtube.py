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
            # Do NOT return here. "No caption track" is the common case, and
            # it is precisely when the local-STT fallback below should run.
            # Returning early made that fallback unreachable except when the
            # package was missing entirely — the one case it could not help.
            logger.debug('caption fetch failed for %s (%s); trying local STT',
                         video_id, exc)
            entries = None

        if entries is None:
            result = _whisper_fallback(video_id, language)
            if result is not None:
                return result
            return {
                'success': False,
                'connection_mechanism': CONNECTION_MECHANISM,
                'video_id': video_id,
                'error': ('no caption track for this video, and local STT '
                          'could not produce one'),
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

    # Last resort: no captions published, so make our own.
    #
    # Most videos worth reading have no caption track — five of six on our own
    # channel have none, which makes a captions-only tool useless exactly where
    # it is most needed. HARTOS already ships local STT (faster-whisper ->
    # sherpa-onnx -> openai-whisper, hardware-tiered, models cached under
    # ~/.hevolve/models/stt). Pull the audio, transcribe it here. No cloud, no
    # per-minute cost, and it works on any video regardless of what the
    # uploader published.
    result = _whisper_fallback(video_id, language)
    if result is not None:
        return result

    return {
        'success': False,
        'connection_mechanism': CONNECTION_MECHANISM,
        'video_id': video_id,
        'error': ('no caption track, and local STT unavailable '
                  '(need yt-dlp + faster-whisper)'),
    }


def _whisper_fallback(video_id: str, language: str = 'en') -> Optional[dict]:
    """Download the audio and transcribe it locally. None if unavailable.

    Returns a dict shaped exactly like the caption path so callers cannot
    tell the difference except by reading `tool` -- the agent asked for the
    words, not for how we got them.
    """
    import os
    import tempfile

    try:
        import yt_dlp
    except ImportError:
        logger.debug('yt-dlp not installed; cannot fall back to local STT')
        return None

    tmpdir = tempfile.mkdtemp(prefix='yt_stt_')
    outtmpl = os.path.join(tmpdir, '%(id)s.%(ext)s')
    try:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])

        audio = None
        for name in os.listdir(tmpdir):
            if name.startswith(video_id):
                audio = os.path.join(tmpdir, name)
                break
        if not audio:
            logger.debug('yt-dlp produced no audio file for %s', video_id)
            return None

        # Reuse the shipped STT dispatcher rather than picking an engine here,
        # so this inherits the same hardware tiering and no-speech gating the
        # voice stack already uses.
        import json as _json
        from integrations.service_tools import whisper_tool
        raw = whisper_tool._transcribe_impl(audio, language)
        parsed = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        text = (parsed.get('text') or '').strip()
        if not text:
            return None
        return {
            'success': True,
            'connection_mechanism': CONNECTION_MECHANISM,
            'tool': 'local_stt',
            'video_id': video_id,
            'language': parsed.get('language') or language,
            'text': text,
            'segment_count': None,
        }
    except Exception as exc:
        logger.debug('local STT fallback failed for %s: %s', video_id, exc)
        return None
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
