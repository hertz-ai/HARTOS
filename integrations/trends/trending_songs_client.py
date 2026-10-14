"""integrations.trends.trending_songs_client: her agent's read of the
trending-song list her own desktop made, for the reel_trending_songs tool
and the song a reel suggests.

Owner ruling A10d ("all in local nunba build not in central"): the list is
the one integrations.trends.daily_job publishes on this computer
(trending_songs.read_latest); nothing is fetched from central or any other
node, and nothing here runs in the background.

* No list -> an empty one whose ``reason`` is daily_job.why_no_list() and
  whose ``note`` says it in words her agent can pass on, and that read just
  as well in the reel's push to her phone.  Above all: no DEDICATED research
  Instagram account is set up on this computer (never the business
  account).  With no list, this read first asks the owner for whatever part
  of that account is missing (daily_job.ask_for_account: the one credential
  consent card, one per part, never again after a no) and the reason says
  where that ask stands: 'account_asked', 'account_declined', or
  'no_account' when nobody here could be asked.  A list the contract refuses
  (contracts.validate_trending_songs: a URL, session data, malformed) is
  never served: read_latest drops it.
* A list kept after a failed day is served marked stale, with the reason;
  a paused one ('challenge', 'login') says how long the pause runs
  (daily_job.FREEZE_S, doubling, at most MAX_FREEZE_S).
* Each song's language: the job's language_hint when it gave one, else the
  title's script (trending_songs.script_language, through the TTS router's
  detector: Tamil, Devanagari, Gurmukhi); only a Latin-script title asks
  this node's own LLM, once per song, and only when that LLM stays on the
  premises (core.autogen_config.llm_stays_on_premises).  Answers are cached
  in trends/song_lang.json.

Output ``hart.trending_songs.client.v1``: ``{'schema', 'fetched_at',
'generated_at', 'date', 'stale', 'reason', 'note', 'songs': [{'song_key',
'title', 'artist', 'lang', 'suggested_start_ms', 'start_label', 'score',
'accounts', 'reels', 'is_original_audio', 'is_explicit', 'duration_ms'}]}``,
best first.  ``fetched_at`` is when the list was read here; ``note`` is
None when there is nothing to explain.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.file_cache import atomic_json_write
from integrations.reel_studio.contracts import TRENDING_CLIENT_SCHEMA, start_label
from integrations.trends import daily_job as _job
from integrations.trends import trending_songs as ts

logger = logging.getLogger(__name__)

LLM_TIMEOUT_S = 20.0
DEFAULT_LIMIT = 10
#: The most Latin titles one call asks the LLM about; the rest wait for a
#: later call (they are asked in rank order, so the top songs come first).
MAX_LLM_ASKS = 8
_MAX_CACHED_LANGS = 2000
#: Language names an answer may use instead of the code.
_LANG_NAMES = {'tamil': 'ta', 'hindi': 'hi', 'punjabi': 'pa', 'english': 'en'}
_LANG_CACHE = 'song_lang.json'

#: The pause after a challenge or a refused sign-in, in words
#: (daily_job.FREEZE_S, doubling on a pause in a row, at most MAX_FREEZE_S).
_PAUSE = ('at least %d hours (longer if it happens again, up to %d hours)'
          % (_job.FREEZE_S // 3600, _job.MAX_FREEZE_S // 3600))

#: What her agent is told when there is no list, or the list is old, by
#: reason (daily_job.why_no_list, or the stale reason the job recorded).
#: Written about nobody in particular ("the business account", "the owner
#: of this computer"), so the reel's push to her phone can carry them too.
_NOTES = {
    'central': ("Trending songs are made on a desktop, never on a server, so this node "
                "keeps no list."),
    'tier_unknown': ("This computer could not tell whether it is a desktop or a server, so "
                     "it makes no trending-song list for now."),
    'no_account': ("No research Instagram account is set up on this computer, so there is no "
                   "trending-song list, and the owner of this computer could not be asked "
                   "for one here. It needs a separate Instagram account made only for this "
                   "research, never the business account."),
    'account_asked': ("No research Instagram account is set up on this computer yet, so there "
                      "is no trending-song list. The owner of this computer is asked on the "
                      "consent card for the username and password of a separate Instagram "
                      "account made only for this research, never the business account."),
    'account_declined': ("The owner of this computer chose not to enter a research Instagram "
                         "account here (or it was turned off after Instagram refused it too "
                         "often), so there is no trending-song list. They can choose \"Allow "
                         "asking again\" for it in Privacy settings."),
    'no_watchlist': ("No Instagram accounts to watch are listed on this computer yet "
                     "(trends/watchlist.json in its data folder), so there is no "
                     "trending-song list."),
    'setup_asked': ("The trending-song reader is waiting for the owner of this computer to "
                    "allow installing it (a card on the desktop)."),
    'setup_declined': ("The owner of this computer chose not to install the trending-song "
                       "reader, so there is no list."),
    'setup_unavailable': ("The trending-song reader cannot be installed: nobody on this "
                          "computer can allow it."),
    'no_installer': ("This computer has no installer for the trending-song reader; it comes "
                     "with the Nunba desktop app."),
    'installing': ("The trending-song reader is being installed on this computer; the first "
                   "list follows within a day."),
    'running': "Today's trending-song list is being made right now; ask again in a few minutes.",
    'not_run_yet': ("The first trending-song list has not been made yet; it is made once a "
                    "day while the computer is not in use."),
    'challenge': ("Instagram asked the research account to prove it is a person, so the "
                  "list is paused for %s; someone should sign in to that account by hand "
                  "meanwhile. Then it tries again on its own." % _PAUSE),
    'login': ("Instagram refused the research account's sign-in, so the list is paused for "
              "%s. The owner of this computer is asked on the consent card to enter its "
              "username and password again; the next try, after the pause, uses what is "
              "entered by then." % _PAUSE),
}

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _lang_cache_path() -> str:
    return os.path.join(ts.trends_dir(), _LANG_CACHE)


# ── the list ──────────────────────────────────────────────────────────────

def _why_no_list() -> str:
    """daily_job.why_no_list(), asked after daily_job.ask_for_account(): with
    any part of her research account missing, its card is filed (or sent
    again to whatever screen is up now; never after the owner's no), so the
    reason says where that ask stands.  ask_for_account asks for nothing
    when the account is entered or the node is not a desktop.  'error' when
    it cannot be told."""
    try:
        _job.ask_for_account()
        return _job.why_no_list()
    except Exception as e:
        logger.warning("trending songs: why there is no list is unknown: %s", e)
        return 'error'


def note_for(reason: Optional[str]) -> Optional[str]:
    """The sentence her agent passes on for ``reason``, or None for none."""
    if not reason:
        return None
    return _NOTES.get(reason) or ('There is no fresh trending-song list on this computer '
                                  '(%s).' % reason)


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
    'other') when given.  Never raises.  With no list on this computer the
    songs are empty and ``reason`` / ``note`` say why (daily_job.why_no_list:
    with no research account set up here, the owner is first asked for one
    on the consent card and the reason is 'account_asked',
    'account_declined' or 'no_account'; else 'central', 'no_watchlist',
    'not_run_yet', ...); a stale list keeps its songs and names the job's
    reason."""
    want = _lang_filter(langs)
    try:
        limit = max(1, min(int(limit), ts.MAX_SONGS))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    with _lock:
        try:
            doc = ts.read_latest()
        except Exception as e:
            logger.warning("trending songs: the list could not be read: %s", e)
            doc = None
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
    if doc is None:
        stale, reason = True, _why_no_list()
    elif doc.get('stale') is True:
        stale = True
        reason = doc.get('stale_reason') if isinstance(doc.get('stale_reason'), str) \
            and doc.get('stale_reason') else 'stale'
    else:
        stale, reason = False, None
    doc = doc or {}
    return {'schema': TRENDING_CLIENT_SCHEMA, 'fetched_at': _iso(_now()),
            'generated_at': doc.get('generated_at'), 'date': doc.get('date'),
            'stale': stale, 'reason': reason, 'note': note_for(reason), 'songs': songs}


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
    try:
        with open(_lang_cache_path(), encoding='utf-8') as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = None
    return {'cache': cache if isinstance(cache, dict) else {}, 'dirty': False,
            'asked': 0, 'failed': False}


def _save_langs(asking: Dict[str, Any]) -> None:
    if asking['dirty']:
        items = list(asking['cache'].items())[-_MAX_CACHED_LANGS:]
        try:
            atomic_json_write(_lang_cache_path(), dict(items))
        except OSError as e:
            logger.warning("trending songs: could not write %s: %s", _LANG_CACHE, e)
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
