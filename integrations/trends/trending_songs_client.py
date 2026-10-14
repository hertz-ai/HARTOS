"""integrations.trends.trending_songs_client: a desktop's read of central's
trending-song list, for her agent's reel_trending_songs tool.

Only when the tool asks: nothing here runs in the background (owner rule:
a desktop agent works in the background only when that earns for or adds
value to its user, and a song list nobody asked for does neither).

* The list comes from central's GET /api/trends/songs/latest, at
  ``SyncEngine.parent_tier_url()`` (the one resolver of "where is my
  parent"), fetched with ``core.http_pool.pooled_get`` (8 s, If-None-Match)
  and cached six hours under ``get_data_dir()/trends/client/``.  Offline,
  the cached list is served marked stale with the reason; with no central
  and no cache, an empty list says why.  A list the contract refuses
  (contracts.validate_trending_songs: a URL, session data, malformed) is
  neither used nor cached.
* Each song's language: central's language_hint when it gave one, else
  the title's script (trending_songs.script_language, through the TTS
  router's detector: Tamil, Devanagari, Gurmukhi); only a Latin-script
  title asks this node's own LLM, once per song, and only when that LLM
  stays on the premises (core.autogen_config.llm_stays_on_premises).
  Answers are cached in song_lang.json.

Output ``hart.trending_songs.client.v1``: ``{'schema', 'fetched_at',
'generated_at', 'date', 'stale', 'reason', 'songs': [{'song_key', 'title',
'artist', 'lang', 'suggested_start_ms', 'start_label', 'score',
'accounts', 'reels', 'is_original_audio', 'is_explicit',
'duration_ms'}]}``, best first.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.file_cache import atomic_json_write
from integrations.reel_studio.contracts import (
    TRENDING_CLIENT_SCHEMA, TRENDING_SCHEMA, start_label, validate_trending_songs)
from integrations.trends import trending_songs as ts

logger = logging.getLogger(__name__)

#: How long a fetched list is used before central is asked again.
CACHE_TTL_S = 6 * 3600.0
FETCH_TIMEOUT_S = 8.0
LLM_TIMEOUT_S = 20.0
DEFAULT_LIMIT = 10
#: The most Latin titles one call asks the LLM about; the rest wait for a
#: later call (they are asked in rank order, so the top songs come first).
MAX_LLM_ASKS = 8
_MAX_CACHED_LANGS = 2000
#: Language names an answer may use instead of the code.
_LANG_NAMES = {'tamil': 'ta', 'hindi': 'hi', 'punjabi': 'pa', 'english': 'en'}

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _client_dir() -> str:
    return os.path.join(ts.trends_dir(), 'client')


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_json(name: str) -> Any:
    try:
        with open(os.path.join(_client_dir(), name), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(name: str, data: Any) -> None:
    try:
        atomic_json_write(os.path.join(_client_dir(), name), data)
    except OSError as e:
        logger.warning("trending songs: could not write %s: %s", name, e)


# ── the list ──────────────────────────────────────────────────────────────

def _read_cache() -> Optional[Dict[str, Any]]:
    cache = _read_json('latest.json')
    if not isinstance(cache, dict) or not isinstance(cache.get('doc'), dict):
        return None
    fetched = cache.get('fetched_at')
    if not isinstance(fetched, (int, float)) or isinstance(fetched, bool):
        return None
    if validate_trending_songs(cache['doc']):
        return None
    return cache


def _central_base() -> str:
    try:
        from integrations.social.sync_engine import SyncEngine
        return (SyncEngine.parent_tier_url() or '').rstrip('/')
    except Exception as e:
        logger.debug("trending songs: no parent tier (%s)", e)
        return ''


def _staleness(doc: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if doc.get('stale') is True:
        reason = doc.get('stale_reason')
        return True, reason if isinstance(reason, str) and reason else 'stale'
    return False, None


def _fallback(cache: Optional[Dict[str, Any]], reason: str):
    if cache:
        return cache['doc'], cache['fetched_at'], True, reason
    return None, None, True, reason


def _current_list():
    """``(doc, fetched_at, stale, reason)``: the cached list while it is
    fresh, else central's (a 304 renews the cache), else the cache marked
    stale with why."""
    cache = _read_cache()
    now = _now()
    if cache and now - cache['fetched_at'] < CACHE_TTL_S:
        return (cache['doc'], cache['fetched_at']) + _staleness(cache['doc'])
    base = _central_base()
    if not base:
        return _fallback(cache, 'no_central')
    headers = {}
    if cache and isinstance(cache.get('etag'), str) and cache['etag']:
        headers['If-None-Match'] = cache['etag']
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(base + ts.SONGS_ROUTE, timeout=FETCH_TIMEOUT_S, headers=headers)
    except Exception as e:
        logger.info("trending songs: central unreachable (%s)", e)
        return _fallback(cache, 'unreachable')
    if resp.status_code == 304 and cache:
        cache['fetched_at'] = now
        _write_json('latest.json', cache)
        return (cache['doc'], now) + _staleness(cache['doc'])
    if resp.status_code != 200:
        return _fallback(cache, 'status_%d' % resp.status_code)
    try:
        body = resp.json()
    except Exception:
        return _fallback(cache, 'invalid_list')
    if (isinstance(body, dict) and body.get('schema') == TRENDING_SCHEMA
            and body.get('generated_at') is None and not body.get('songs')):
        return _fallback(cache, 'not_published')
    if not isinstance(body, dict) or body.get('schema') != TRENDING_SCHEMA:
        return _fallback(cache, 'invalid_list')
    problems = validate_trending_songs(body)
    if problems:
        logger.warning("trending songs: central's list refused: %s", '; '.join(problems[:3]))
        return _fallback(cache, 'invalid_list')
    etag = resp.headers.get('ETag') if hasattr(resp, 'headers') else None
    _write_json('latest.json', {'fetched_at': now, 'etag': etag if isinstance(etag, str) else None,
                                'doc': body})
    return (body, now) + _staleness(body)


def _lang_filter(langs: Any) -> Optional[set]:
    if isinstance(langs, str):
        langs = langs.split(',')
    if not isinstance(langs, (list, tuple, set)):
        return None
    tags = {t.strip().lower() for t in langs if isinstance(t, str) and t.strip()}
    return {t for t in tags if t in ts.TITLE_LANGS} or None


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _shape(song: Dict[str, Any], lang: Optional[str]) -> Dict[str, Any]:
    start = _int(song.get('suggested_start_ms'))
    start = start if start is not None and start >= 0 else 0
    return {'song_key': song['song_key'], 'title': song.get('title') or '',
            'artist': song.get('artist') or '', 'lang': lang,
            'suggested_start_ms': start, 'start_label': start_label(start),
            'score': song.get('score'), 'accounts': _int(song.get('accounts')),
            'reels': _int(song.get('reels')),
            'is_original_audio': song.get('is_original_audio') is True,
            'is_explicit': song.get('is_explicit') is True,
            'duration_ms': _int(song.get('duration_ms'))}


def get_trending_songs(langs: Any = None, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """The trending songs for her agent, best first: at most ``limit``
    (1 to trending_songs.MAX_SONGS), only those in ``langs`` (a code, a
    comma-separated text or a list of them; contracts.STYLE_LANGUAGES plus
    'other') when given.  Never raises; an empty list says why in
    ``reason`` ('no_central', 'unreachable', 'not_published',
    'invalid_list', 'status_<n>', or central's own stale reason)."""
    want = _lang_filter(langs)
    try:
        limit = max(1, min(int(limit), ts.MAX_SONGS))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    with _lock:
        try:
            doc, fetched_at, stale, reason = _current_list()
        except Exception as e:
            logger.warning("trending songs: list unavailable: %s", e)
            doc, fetched_at, stale, reason = None, None, True, 'error'
        songs: List[Dict[str, Any]] = []
        asking = _asking()
        for song in (doc or {}).get('songs') or []:
            hint = song.get('language_hint')
            lang = hint if hint in ts.TITLE_LANGS else _tag(song, asking)
            if want and lang not in want:
                continue
            songs.append(_shape(song, lang))
            if len(songs) >= limit:
                break
        _save_langs(asking)
    doc = doc or {}
    return {'schema': TRENDING_CLIENT_SCHEMA,
            'fetched_at': _iso(fetched_at if fetched_at is not None else _now()),
            'generated_at': doc.get('generated_at'), 'date': doc.get('date'),
            'stale': bool(stale), 'reason': reason, 'songs': songs}


# ── a song's language ─────────────────────────────────────────────────────

def _parse_lang(text: Any) -> Optional[str]:
    for word in re.findall(r'[a-z]+', text.lower() if isinstance(text, str) else ''):
        if word in ts.TITLE_LANGS:
            return word
        if word in _LANG_NAMES:
            return _LANG_NAMES[word]
    return None


def _ask_llm(song: Dict[str, Any], asking: Dict[str, Any]) -> Optional[str]:
    """This node's own LLM's answer for a Latin-script title, or None (off
    the premises, the call's ask budget spent, the model failing, or an
    answer outside the set).  The first failure stops asking for the rest
    of the call."""
    if asking['failed'] or asking['asked'] >= MAX_LLM_ASKS:
        return None
    from core.autogen_config import llm_http_target, llm_stays_on_premises
    if not llm_stays_on_premises():
        return None
    asking['asked'] += 1
    title = ' '.join(str(song.get('title') or '').split())[:120]
    artist = ' '.join(str(song.get('artist') or '').split())[:80]
    prompt = ('Song title: %s\nArtist: %s\nWhich language are the lyrics in? Answer with '
              'one code only, from: %s.' % (title, artist or 'unknown',
                                            ', '.join(ts.TITLE_LANGS)))
    try:
        from core.constants import LLM_THINKING_OFF_KWARGS
        from core.http_pool import pooled_post
        url, headers, model = llm_http_target()
        resp = pooled_post(url, headers=headers, timeout=LLM_TIMEOUT_S, json={
            'model': model,
            'messages': [{'role': 'system',
                          'content': 'You name the language of a song from its title.'},
                         {'role': 'user', 'content': prompt}],
            'temperature': 0, 'max_tokens': 8,
            'chat_template_kwargs': dict(LLM_THINKING_OFF_KWARGS)})
        if resp.status_code != 200:
            raise RuntimeError('HTTP %s' % resp.status_code)
        content = (resp.json().get('choices') or [{}])[0].get('message', {}).get('content')
    except Exception as e:
        asking['failed'] = True
        logger.info("trending songs: language not asked of the local model (%s)", e)
        return None
    return _parse_lang(content)


def _asking() -> Dict[str, Any]:
    """One call's language state: the song_lang.json cache, whether it
    changed, and the LLM ask budget."""
    cache = _read_json('song_lang.json')
    return {'cache': cache if isinstance(cache, dict) else {}, 'dirty': False,
            'asked': 0, 'failed': False}


def _save_langs(asking: Dict[str, Any]) -> None:
    if asking['dirty']:
        items = list(asking['cache'].items())[-_MAX_CACHED_LANGS:]
        _write_json('song_lang.json', dict(items))
        asking['dirty'] = False


def _tag(song: Dict[str, Any], asking: Dict[str, Any]) -> Optional[str]:
    key = song.get('song_key') if isinstance(song.get('song_key'), str) else None
    cache = asking['cache']
    if key and cache.get(key) in ts.TITLE_LANGS:
        return cache[key]
    title = song.get('title')
    lang = ts.script_language(title)
    if lang is None and ts.is_latin_title(title):
        lang = _ask_llm(song, asking)
    if key and lang in ts.TITLE_LANGS:
        cache[key] = lang
        asking['dirty'] = True
    return lang


def tag_language(song: Dict[str, Any]) -> Optional[str]:
    """One song's language: 'ta', 'hi', 'pa', 'en', 'other', or None when
    it cannot be told.  Cached per song_key.  A native-script title is read
    from its script; only a Latin-script title asks the local LLM, and
    only when that LLM stays on the premises."""
    if not isinstance(song, dict):
        return None
    with _lock:
        asking = _asking()
        lang = _tag(song, asking)
        _save_langs(asking)
    return lang
