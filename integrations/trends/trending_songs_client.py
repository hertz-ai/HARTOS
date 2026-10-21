"""integrations.trends.trending_songs_client: her agent's read of the
trending-song list her own desktop made, for the reel_trending_songs tool
and the song a reel suggests.

Owner ruling A10d ("all in local nunba build not in central"): the list is
the one integrations.trends.daily_job publishes on this computer
(trending_songs.read_latest); nothing is fetched from central or any other
node, and nothing here runs in the background.

* No list -> an empty one whose ``reason`` is daily_job.why_no_list() and
  whose ``note`` says it in words her agent can pass on, and that read just
  as well in the reel's push to her phone (never a code or a file path).
  On a default install that is 'no_watchlist': no Instagram accounts are
  chosen to watch (her agent's reel_watchlist tool names them, owner ruling
  A17b), so nothing is asked for and the note says naming the accounts to
  track comes first and to choose a song in Instagram meanwhile.  With a
  watchlist, the next thing missing
  is the DEDICATED research Instagram account (never the business
  account): this read first asks the owner for whatever part of it is
  missing (daily_job.ask_for_account: the one credential consent card, one
  per part, never again after a no; nothing while there is no usable
  watchlist) and the reason says where that ask stands: 'account_asked',
  'account_declined', or 'no_account' when nobody here could be asked.
  A list the contract refuses
  (contracts.validate_trending_songs: a URL, session data, malformed) is
  never served: read_latest drops it.
* A list kept after a failed day is served marked stale, with the reason;
  a paused one ('challenge', 'login') says how long the pause runs
  (daily_job.FREEZE_S, doubling, at most MAX_FREEZE_S).
* A list made more than OLD_AFTER_S ago that nothing marked stale (the
  computer never idle long enough, the job stopped) is never presented as
  current (critic item 2.6): it is served marked stale, reason 'old_list'.
  Every stale list's note ends saying how old the list is and from which
  day, and ``age_hours`` carries it for any list.
* ``list_status()``: the same reason, words and age without the songs (no
  song is tagged, so no model is asked), for a reply that needs only where
  the list stands (the reel_watchlist tool's).
* Each song's language: the job's language_hint when it gave one, else the
  title's script (trending_songs.script_language, through the TTS router's
  detector: Tamil, Devanagari, Gurmukhi); only a Latin-script title asks
  this node's own LLM, once per song, and only when that LLM stays on the
  premises (core.autogen_config.llm_stays_on_premises).  Answers are cached
  in trends/song_lang.json.  When this node's own llama-server refuses the
  connection (no API endpoint configured), the model orchestrator's existing
  ensure_loaded_async('llm') is asked once for the call (amendment A13);
  a busy or erroring server is never asked to start.

Output ``hart.trending_songs.client.v1``: ``{'schema', 'fetched_at',
'generated_at', 'date', 'age_hours', 'stale', 'reason', 'note', 'songs':
[{'song_key', 'title', 'artist', 'lang', 'suggested_start_ms',
'start_label', 'score', 'accounts', 'reels', 'is_original_audio',
'is_explicit', 'duration_ms'}]}``, best first.  ``fetched_at`` is when the
list was read here; ``age_hours`` how long ago it was made (None with no
list); ``note`` is None when there is nothing to explain.
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
#: A list made longer ago than this is not presented as current.  The job
#: makes the next one a day after its last good run (daily_job.DAY_S, plus
#: up to JITTER_S) once the computer is idle, so a list older than two days
#: means at least one day's run is missing.
OLD_AFTER_S = 2 * _job.DAY_S
#: How the published list writes its time (daily_job._iso), and its day.
_MADE_FORMATS = (('generated_at', '%Y-%m-%dT%H:%M:%SZ'), ('date', '%Y-%m-%d'))
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

#: The words shared by the run errors that end the same way for her
#: (daily_job._failed, ig_worker, sources.collect, trending_songs.
#: why_not_published): what happened, and that the job tries again itself.
_INSTAGRAM_UNANSWERED = ("Instagram did not answer well enough today to make a fresh "
                         "trending-song list; this computer tries again on its own later.")
_READER_FAILED = ("The trending-song reader on this computer ran into a problem, so the "
                  "list was not made fresh; it tries again on its own later.")
_READER_UNINSTALLED = ("Installing the trending-song reader on this computer did not "
                       "finish, so there is no fresh trending-song list; it is installed "
                       "again on its own later.")
_SLOWED = ("Instagram asked the research account to slow down, so the trending-song list "
           "was not made fresh today; it tries again on its own after a pause of at least "
           "%d hours." % (_job.COOLDOWN_S // 3600))
#: A reason no entry below names (one added later): said without its code.
_NO_FRESH_LIST = ("There is no fresh trending-song list on this computer right now; a new "
                  "one is made on its own.")

#: What her agent is told when there is no list, or the list is old, by
#: reason (daily_job.why_no_list, or the stale reason the job recorded).
#: Written about nobody in particular ("the business account", "the owner
#: of this computer"), so the reel's push to her phone can carry them too,
#: and never naming a mechanism code or a file: the ``reason`` carries the
#: code.
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
    'no_watchlist': ("Trending songs are not set up on this computer yet: no Instagram "
                     "accounts are chosen for it to watch, so there is no trending-song list "
                     "to suggest a song from. Naming the Instagram accounts to track is the "
                     "first step; choose a song in Instagram for now."),
    'no_disk': ("This computer has too little free disk space to install the trending-song "
                "reader (it needs about %d MB free), so there is no trending-song list; it "
                "tries again on its own once there is room."
                % (_job.INSTALL_FREE_BYTES // (1024 * 1024))),
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
    'throttled': _SLOWED,
    'feedback': _SLOWED,
    'transport': _INSTAGRAM_UNANSWERED,
    'not_found': _INSTAGRAM_UNANSWERED,
    'partial_run': _INSTAGRAM_UNANSWERED,
    'empty_list': ("Today's run found no songs in the watched accounts' reels, so the "
                   "earlier trending-song list is kept; it tries again on its own later."),
    'unknown': _READER_FAILED,
    'worker_failed': _READER_FAILED,
    'bad_output': _READER_FAILED,
    'spawn_failed': _READER_FAILED,
    'timeout': _READER_FAILED,
    'interrupted': _READER_FAILED,
    'invalid_list': _READER_FAILED,
    'not_written': _READER_FAILED,
    'dependency': _READER_UNINSTALLED,
    'install_failed': _READER_UNINSTALLED,
    'install_timeout': _READER_UNINSTALLED,
    'stale': ("The trending-song list on this computer is not from today; a fresh one is "
              "made on its own."),
    'old_list': ("The trending-song list on this computer has not been made fresh for more "
                 "than %d days, so it may not show what is trending now."
                 % (OLD_AFTER_S // _job.DAY_S)),
    'error': "Whether this computer has a trending-song list could not be checked just now.",
}
#: What every stale list's note ends with: how old the list is, and its day.
_AGE_NOTE = "This list was made %s ago, on %s."

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
    when the account is entered, the node is not a desktop, or no usable
    watchlist exists.  'error' when it cannot be told."""
    try:
        _job.ask_for_account()
        return _job.why_no_list()
    except Exception as e:
        logger.warning("trending songs: why there is no list is unknown: %s", e)
        return 'error'


def note_for(reason: Optional[str]) -> Optional[str]:
    """The sentence her agent passes on for ``reason``, or None for none.
    It never carries the reason's code (it rides the reel's push to her
    phone); a reason no entry names gets the general _NO_FRESH_LIST."""
    if not reason:
        return None
    return _NOTES.get(reason) or _NO_FRESH_LIST


def _read() -> Optional[dict]:
    """The published list (trending_songs.read_latest), or None when there
    is none or it could not be read (logged)."""
    try:
        return ts.read_latest()
    except Exception as e:
        logger.warning("trending songs: the list could not be read: %s", e)
        return None


def _made_at(doc: dict) -> Optional[float]:
    """When the list was made, in epoch seconds: its generated_at, else its
    date at 00:00 UTC (_MADE_FORMATS); None when neither reads."""
    for key, fmt in _MADE_FORMATS:
        try:
            made = datetime.strptime(doc.get(key), fmt)
        except (TypeError, ValueError):
            continue
        return made.replace(tzinfo=timezone.utc).timestamp()
    return None


def _age_words(age_s: float) -> str:
    """How old, in words: hours under two days, then days."""
    hours = int(age_s // 3600)
    if hours < 1:
        return 'less than an hour'
    if hours < 48:
        return '1 hour' if hours == 1 else '%d hours' % hours
    return '%d days' % int(age_s // _job.DAY_S)


def _standing(doc: Optional[dict], now: float) -> Dict[str, Any]:
    """``{'stale', 'reason', 'note', 'age_hours'}`` for ``doc`` (the
    published list, or None) at ``now``.  No list: why (_why_no_list, which
    first asks the owner for the research account when that is why).  A
    list the job marked stale: its reason.  A list made more than
    OLD_AFTER_S ago: 'old_list'.  Else it is current, with no reason and no
    note.  A stale list's note ends with how old it is and its day."""
    if doc is None:
        reason = _why_no_list()
        return {'stale': True, 'reason': reason, 'note': note_for(reason), 'age_hours': None}
    made = _made_at(doc)
    age = None if made is None else max(0.0, now - made)
    hours = None if age is None else int(age // 3600)
    marked = doc.get('stale_reason')
    if doc.get('stale') is True:
        reason = marked if isinstance(marked, str) and marked else 'stale'
    elif age is not None and age > OLD_AFTER_S:
        reason = 'old_list'
    else:
        return {'stale': False, 'reason': None, 'note': None, 'age_hours': hours}
    note = note_for(reason)
    if age is not None:
        note += ' ' + _AGE_NOTE % (_age_words(age), doc.get('date'))
    return {'stale': True, 'reason': reason, 'note': note, 'age_hours': hours}


def list_status() -> Dict[str, Any]:
    """Where this computer's trending-song list stands, without its songs:
    ``{'stale', 'reason', 'note', 'age_hours', 'date'}`` as
    get_trending_songs answers them (with no list, the owner is first asked
    for the research account when that is what is missing).  No song is
    tagged, so no model is asked.  Never raises."""
    doc = _read()
    out = _standing(doc, _now())
    out['date'] = (doc or {}).get('date')
    return out


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
    'no_watchlist' on a default install, and nothing is asked; with a
    watchlist but no research account set up here, the owner is first asked
    for one on the consent card and the reason is 'account_asked',
    'account_declined' or 'no_account'; else 'central', 'not_run_yet', ...);
    a stale list keeps its songs and names the job's reason, and so does a
    list older than OLD_AFTER_S ('old_list'), each saying how old it is
    (_standing)."""
    want = _lang_filter(langs)
    try:
        limit = max(1, min(int(limit), ts.MAX_SONGS))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    with _lock:
        doc = _read()
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
    now = _now()
    standing = _standing(doc, now)
    doc = doc or {}
    return {'schema': TRENDING_CLIENT_SCHEMA, 'fetched_at': _iso(now),
            'generated_at': doc.get('generated_at'), 'date': doc.get('date'),
            'age_hours': standing['age_hours'], 'stale': standing['stale'],
            'reason': standing['reason'], 'note': standing['note'], 'songs': songs}


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
        # Its own llama-server refused the connection (down, not busy): the
        # existing ensure brings it up for the next read, as Nunba's /chat does.
        import requests
        from core.autogen_config import configured_api_endpoint
        if isinstance(e, requests.ConnectionError) and not configured_api_endpoint():
            try:
                from integrations.service_tools.model_orchestrator import get_orchestrator
                get_orchestrator().ensure_loaded_async('llm', caller='trending_songs')
            except Exception as ensure_error:
                logger.warning("trending songs: the local model was not asked to start (%s)",
                               ensure_error)
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
