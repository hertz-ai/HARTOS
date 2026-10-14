"""integrations.trends.trending_songs: her desktop's trending-song list.

A day's batch from the trends worker (sources.BATCH_SCHEMA) becomes the
``hart.trending_songs.v1`` list her agent suggests songs from, published on
her own desktop by daily_job and read there by trending_songs_client.

* ``normalize_reel``: one reel, as the private API sends it or as
  instagrapi's Media model dumps it, read into one record.
* ``rank_batch``: the batch ranked.  The maths is igkit.swipe's, the one
  implementation of it (Gate 4): each reel's multiple over its own
  account's median of the newest 12 reels (no median under 6), each song
  scored by summing, over the distinct accounts that used it, that
  account's best ``weight * log1p(multiple)``.  This module adds which
  reels count (the last WINDOW_DAYS for songs, every reel for the median),
  the recency weight (half-life HALF_LIFE_DAYS), the dedupe by reel, the
  joining of a song's names (canonical id, attribution audio id, cluster),
  the song's title and start, and its language hint.
* ``merge_days``: today's ranking with yesterday's list, so a song keeps
  the day it was first seen.
* ``publish`` / ``mark_stale`` / ``read_latest``: the one writer and reader
  of ``get_data_dir()/trends/songs/{latest,YYYY-MM-DD}.json``.

Nothing here talks to Instagram or imports instagrapi.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.config_cache import flag_value
from core.file_cache import atomic_json_write
from core.platform_paths import get_data_dir
from integrations.reel_studio.contracts import (
    STYLE_LANGUAGES, TRENDING_SCHEMA, validate_trending_songs)
from integrations.reel_studio.igkit.swipe import account_median, rank_reels
from integrations.trends.sources import as_dict, clean_id, iso_now, music_of

logger = logging.getLogger(__name__)

#: Where the songs come from when the batch does not say.
SOURCE = 'instagrapi'
#: Reels newer than this count toward songs (every reel counts toward its
#: account's median), and a reel this old weighs half (goals-trends 2.3).
WINDOW_DAYS = 7
HALF_LIFE_DAYS = 3.5
#: Most songs published, and example reels kept per song.
MAX_SONGS = 50
MAX_EXAMPLES = 3
#: Days of YYYY-MM-DD.json kept beside latest.json.
KEEP_DAYS = 30
#: A run where fewer than this share of the watched accounts answered is
#: not published (goals-trends 2.5).
MIN_OK_PERCENT = 60
#: A title in no language she writes in.
LANG_OTHER = 'other'
#: The language tags a song can carry: hers (contracts.STYLE_LANGUAGES) or
#: 'other'.
TITLE_LANGS = tuple(STYLE_LANGUAGES) + (LANG_OTHER,)

_DAY_S = 86400.0
_SCRIPT_LANGS = tuple(lang for lang in STYLE_LANGUAGES if lang != 'en')
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_DAY_FILE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\.json')
_TAG_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,31}')
_write_lock = threading.Lock()


# ── one reel ──────────────────────────────────────────────────────────────

def _epoch(value: Any) -> Optional[float]:
    """Seconds since the epoch from an epoch number, an ISO text (Media's
    JSON dump writes '2025-10-12T20:13:20Z') or a datetime; naive times are
    UTC.  None when unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _plays(data: Mapping) -> Optional[int]:
    """The reel's plays: play_count, else video_play_count, view_count,
    video_view_count.  A 0 is no count: instagrapi's extract_media_v1
    writes 0 when the API sent none, and a reel's true plays are never 0
    (a 0 would drag its account's median down)."""
    for key in ('play_count', 'video_play_count', 'view_count', 'video_view_count'):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def normalize_reel(raw: Any, *, account: Optional[str] = None) -> Optional[dict]:
    """One reel as a record, or None when ``raw`` is not a reel (no id).

    ``raw`` is a dict or a pydantic model (anything with model_dump), whole
    or cut down by sources.slim_reel; both read the same.  ``account`` is
    the watched handle the reel came from, used when the reel names no
    owner.  The song is read by sources.music_of; the yes/no flags with
    core.config_cache.flag_value.

    Returns ``{'media_pk', 'code', 'account', 'taken_at' (epoch s or None),
    'plays' (int or None), 'song_key', 'audio_cluster_id',
    'audio_asset_id', 'asset_ids', 'music_canonical_id', 'title', 'artist',
    'highlight_start_ms', 'duration_ms', 'is_original_audio',
    'is_explicit', 'should_mute', 'needs_track'}``; no URL or manifest is
    ever copied into it.
    """
    data = as_dict(raw)
    if not data:
        return None
    pk = clean_id(data.get('pk'))
    if pk is None:
        return None
    user = as_dict(data.get('user')) or {}
    owner = user.get('username')
    if isinstance(owner, str) and owner.strip():
        handle: Optional[str] = owner.strip()
    elif isinstance(account, str) and account.strip():
        handle = account.strip()
    else:
        handle = None
    music = music_of(data)
    return {
        'media_pk': pk,
        'code': clean_id(data.get('code')) or '',
        'account': handle,
        'taken_at': _epoch(data.get('taken_at')),
        'plays': _plays(data),
        'song_key': music['song_key'],
        'audio_cluster_id': music['audio_cluster_id'],
        'audio_asset_id': music['audio_asset_id'],
        'asset_ids': list(music['asset_ids']),
        'music_canonical_id': music['music_canonical_id'],
        'title': music['title'],
        'artist': music['artist'],
        'highlight_start_ms': list(music['highlight_start_ms']),
        'duration_ms': music['duration_ms'],
        'is_original_audio': (music['kind'] == 'original'
                              or flag_value(music['uses_original_audio'], False)),
        'is_explicit': flag_value(music['explicit'], False),
        'should_mute': any(flag_value(v, False) for v in music['mute_flags']),
        'needs_track': music['needs_track'],
    }


def _letters(title: Any) -> List[str]:
    return [ch for ch in title if ch.isalpha()] if isinstance(title, str) else []


def is_latin_title(title: Any) -> bool:
    """True when at least half of the title's letters are Latin: its script
    cannot say whether the song is Tamil, Hindi, Punjabi or English."""
    letters = _letters(title)
    latin = sum(1 for ch in letters if unicodedata.name(ch, '').startswith('LATIN'))
    return bool(letters) and latin * 2 >= len(letters)


def script_language(title: Any) -> Optional[str]:
    """A song's language from its title's script alone: 'ta', 'hi' or 'pa'
    (her languages written in their own scripts), 'other' for any other
    script, None for a Latin-script title (is_latin_title: the script
    cannot tell; trending_songs_client may ask the local LLM), a title with no
    letters, or one the detector cannot place.  The detector is the one the
    TTS router uses (integrations.channels.media.tts_router.detect_language)."""
    if not _letters(title) or is_latin_title(title):
        return None
    try:
        from integrations.channels.media.tts_router import detect_language
        lang = detect_language(title)
    except Exception as e:
        logger.debug("trending songs: no language for %r: %s", title[:40], e)
        return None
    if lang in _SCRIPT_LANGS:
        return lang
    return None if lang == 'en' else LANG_OTHER


# ── a day's ranking ───────────────────────────────────────────────────────

def _songs_of(doc: Any) -> List[Mapping]:
    songs = doc.get('songs') if isinstance(doc, Mapping) else None
    if not isinstance(songs, (list, tuple)):
        return []
    return [s for s in songs
            if isinstance(s, Mapping) and isinstance(s.get('song_key'), str) and s['song_key']]


def known_song_keys(previous: Any) -> List[str]:
    """Every name the published list already resolves: each song's key,
    'canonical:<id>' for its music_canonical_id and 'audio:<id>' for its
    audio asset.  The worker looks up no track for these (daily_job puts
    them in its spec), and rank_batch joins them through ``previous``."""
    keys: List[str] = []
    for song in _songs_of(previous):
        keys.append(song['song_key'])
        canonical = clean_id(song.get('music_canonical_id'))
        if canonical:
            keys.append('canonical:' + canonical)
        asset = clean_id(song.get('audio_asset_id'))
        if asset:
            keys.append('audio:' + asset)
    return list(dict.fromkeys(keys))


def _info(title, artist, highlights, duration, explicit, original, canonical, asset,
          cluster) -> dict:
    return {'title': title or '', 'artist': artist or '',
            'highlight_start_ms': list(highlights or []), 'duration_ms': duration,
            'is_explicit': bool(explicit), 'is_original_audio': bool(original),
            'music_canonical_id': canonical, 'audio_asset_id': asset,
            'audio_cluster_id': cluster}


def _info_from_record(rec: Mapping) -> dict:
    return _info(rec['title'], rec['artist'], rec['highlight_start_ms'], rec['duration_ms'],
                 rec['is_explicit'], rec['is_original_audio'], rec['music_canonical_id'],
                 rec['audio_asset_id'], rec['audio_cluster_id'])


def _info_from_track(track: Any) -> Optional[Tuple[dict, Tuple[str, ...]]]:
    """A looked-up track read by the same reader as a reel's
    music_asset_info (it is that object): its info and its asset ids."""
    music = music_of({'clips_metadata': {'music_info': {'music_asset_info': track}}})
    if not music['audio_cluster_id']:
        return None
    return (_info(music['title'], music['artist'], music['highlight_start_ms'],
                  music['duration_ms'], flag_value(music['explicit'], False), False,
                  music['music_canonical_id'], music['audio_asset_id'],
                  music['audio_cluster_id']),
            tuple(music['asset_ids']))


def _info_from_song(song: Mapping) -> dict:
    def text(key):
        value = song.get(key)
        return value if isinstance(value, str) else ''
    highlights = song.get('highlight_start_ms')
    highlights = [h for h in highlights if isinstance(h, int) and not isinstance(h, bool)
                  and h >= 0] if isinstance(highlights, list) else []
    duration = song.get('duration_ms')
    return _info(text('title'), text('artist'), highlights,
                 duration if isinstance(duration, int) and not isinstance(duration, bool)
                 else None,
                 song.get('is_explicit') is True, song.get('is_original_audio') is True,
                 clean_id(song.get('music_canonical_id')), clean_id(song.get('audio_asset_id')),
                 clean_id(song.get('audio_cluster_id')))


def _song_index(by_account: Mapping[str, List[dict]], tracks: Any,
                previous: Any) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """``(aliases, infos)``: which key each other name of a song resolves to
    ('canonical:<id>' and 'audio:<asset>' to 'cluster:<id>'), and what is
    known of each song.  Evidence order, weakest first: yesterday's list,
    this batch's reels, the tracks the worker looked up."""
    aliases: Dict[str, str] = {}
    prev_infos: Dict[str, dict] = {}
    for song in _songs_of(previous):
        key = song['song_key']
        prev_infos.setdefault(key, _info_from_song(song))
        canonical = clean_id(song.get('music_canonical_id'))
        if canonical:
            aliases['canonical:' + canonical] = key
        asset = clean_id(song.get('audio_asset_id'))
        if asset and key != 'audio:' + asset:
            aliases['audio:' + asset] = key
    reel_infos: Dict[str, dict] = {}
    for records in by_account.values():
        for rec in records:
            key = rec['song_key']
            if not key:
                continue
            if key.startswith('cluster:'):
                for asset in rec['asset_ids']:
                    aliases['audio:' + asset] = key
                if rec['music_canonical_id']:
                    aliases['canonical:' + rec['music_canonical_id']] = key
            current = reel_infos.get(key)
            if current is None or (rec['title'] and not current['title']):
                reel_infos[key] = _info_from_record(rec)
    track_infos: Dict[str, dict] = {}
    for key, track in (tracks.items() if isinstance(tracks, Mapping) else ()):
        if not isinstance(key, str) or not key.startswith('canonical:'):
            continue
        read = _info_from_track(track)
        if read is None:
            continue
        info, asset_ids = read
        target = 'cluster:' + info['audio_cluster_id']
        aliases[key] = target
        for asset in asset_ids:
            aliases['audio:' + asset] = target
        track_infos[target] = info
    infos = dict(prev_infos)
    infos.update(reel_infos)
    infos.update(track_infos)
    return aliases, infos


def _records_by_account(rows: List[Mapping]) -> Dict[str, List[dict]]:
    """The batch's reels as records, grouped by the watched account whose
    page they came from (that page is the account's baseline), each reel
    once across the whole batch."""
    seen = set()
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        if row.get('ok') is not True or not isinstance(row.get('reels'), list):
            continue
        handle = row.get('handle') if isinstance(row.get('handle'), str) else None
        for raw in row['reels']:
            rec = normalize_reel(raw, account=handle)
            if rec is None or rec['media_pk'] in seen:
                continue
            seen.add(rec['media_pk'])
            grouped.setdefault(handle or rec['account'] or '', []).append(rec)
    return grouped


def _song_problems(song: dict) -> List[str]:
    """The contract's verdict on one song (validate_trending_songs on a
    one-song list), so the merged list always validates: a title that is a
    URL drops that song, not the day's list."""
    return validate_trending_songs({'schema': TRENDING_SCHEMA, 'date': '2000-01-01',
                                    'generated_at': 'check', 'source': 'check',
                                    'window_days': 1, 'stale': False, 'songs': [song]})


def _song(top: Mapping, reels: List[Mapping], info: Optional[Mapping], key: str) -> dict:
    info = info or {}
    highlights = list(info.get('highlight_start_ms') or [])
    start = highlights[0] if highlights else (top.get('best_start_ms') or 0)
    cluster = key.split(':', 1)[1] if key.startswith('cluster:') else info.get('audio_cluster_id')
    best = sorted(reels, key=lambda r: (-(r.get('outlier') or 0), r.get('code') or ''))
    title = info.get('title') or ''
    return {
        'song_key': key,
        'audio_cluster_id': cluster,
        'audio_asset_id': info.get('audio_asset_id'),
        'music_canonical_id': info.get('music_canonical_id'),
        'title': title,
        'artist': info.get('artist') or '',
        'is_original_audio': bool(info.get('is_original_audio')) or key.startswith('original:'),
        'is_explicit': bool(info.get('is_explicit')),
        'duration_ms': info.get('duration_ms'),
        'highlight_start_ms': highlights,
        'suggested_start_ms': int(start),
        'language_hint': script_language(title),
        'score': top['score'],
        'accounts': top['accounts'],
        'reels': top['n_reels'],
        'max_outlier': top['max_outlier'],
        'median_outlier': top['median_outlier'],
        'total_plays': int(sum(r['views'] for r in reels)),
        'examples': [{'code': r['code'], 'plays': r['views'], 'outlier': r['outlier']}
                     for r in best if r.get('code')][:MAX_EXAMPLES],
    }


def rank_batch(batch: Any, *, now: Optional[float] = None, previous: Any = None,
               window_days: int = WINDOW_DAYS,
               half_life_days: float = HALF_LIFE_DAYS) -> dict:
    """The day's songs from a worker batch, best first.

    ``previous`` is yesterday's published list: it names songs the worker
    did not look up again (known_song_keys).  Accounts with fewer than six
    counted reels are not ranked.  Songs that end up with no title, or that
    the contract refuses, are left out and counted.

    Returns ``{'source', 'window_days', 'accounts_ok', 'accounts_total',
    'accounts_ranked', 'dropped': {'should_mute', 'no_music', 'no_views',
    'untitled', 'invalid'}, 'songs': [...]}``, each song shaped as in the
    published list (without first_seen / last_seen, which merge_days adds).
    Anything that is not a batch ranks nothing.
    """
    now = time.time() if now is None else float(now)
    dropped = {'should_mute': 0, 'no_music': 0, 'no_views': 0, 'untitled': 0, 'invalid': 0}
    out: Dict[str, Any] = {'source': SOURCE, 'window_days': int(window_days),
                           'accounts_ok': 0, 'accounts_total': 0, 'accounts_ranked': 0,
                           'dropped': dropped, 'songs': []}
    if not isinstance(batch, Mapping) or not isinstance(batch.get('accounts'), list):
        return out
    source = batch.get('source')
    if isinstance(source, str) and _TAG_RE.fullmatch(source.strip()):
        out['source'] = source.strip()
    rows = [r for r in batch['accounts'] if isinstance(r, Mapping)]
    out['accounts_total'] = len(rows)
    out['accounts_ok'] = sum(1 for r in rows if r.get('ok') is True)

    by_account = _records_by_account(rows)
    aliases, infos = _song_index(by_account, batch.get('tracks'), previous)
    reel_rows: List[dict] = []
    for account, records in by_account.items():
        records.sort(key=lambda r: -(r['taken_at'] or 0.0))         # newest first
        median = account_median([r['plays'] for r in records])
        if median is None:
            continue
        out['accounts_ranked'] += 1
        for rec in records:
            if rec['taken_at'] is None or now - rec['taken_at'] > window_days * _DAY_S:
                continue
            if rec['song_key'] is None:
                dropped['no_music'] += 1
                continue
            key = aliases.get(rec['song_key'], rec['song_key'])
            age_days = max(0.0, (now - rec['taken_at']) / _DAY_S)
            reel_rows.append({
                'views': rec['plays'], 'account': account, 'median': median,
                'song_id': key, 'should_mute': rec['should_mute'],
                'highlight_start_ms': (infos.get(key) or {}).get('highlight_start_ms')
                or rec['highlight_start_ms'],
                'weight': 0.5 ** (age_days / half_life_days), 'code': rec['code'],
            })

    ranked = rank_reels(reel_rows)
    dropped['should_mute'] += ranked['dropped']['should_mute']
    dropped['no_views'] += ranked['dropped']['no_views']
    reels_of: Dict[str, List[Mapping]] = {}
    for reel in ranked['reels']:
        reels_of.setdefault(str(reel['song_id']), []).append(reel)
    songs: List[dict] = []
    for top in ranked['top_songs']:
        key = top['song_id']
        song = _song(top, reels_of.get(key, []), infos.get(key), key)
        if not song['title']:
            dropped['untitled'] += 1
            continue
        problems = _song_problems(song)
        if problems:
            dropped['invalid'] += 1
            logger.warning("trending songs: %s left out: %s", key, '; '.join(problems[:3]))
            continue
        songs.append(song)
        if len(songs) >= MAX_SONGS:
            break
    out['songs'] = songs
    return out


def merge_days(batch_rank: Mapping, prev_doc: Any, *, today: str,
               generated_at: Optional[str] = None, region: Optional[str] = None,
               niche: Optional[str] = None) -> dict:
    """The ``hart.trending_songs.v1`` list for ``today`` ('YYYY-MM-DD') from
    rank_batch's result.  A song in ``prev_doc`` keeps its first_seen;
    every song in today's list is last_seen today.  A song that dropped out
    of today's ranking is not carried.  ``region`` and ``niche`` (from the
    watchlist) are written when they are short tags.  Pure; raises
    ValueError for a ``today`` that is not a date."""
    if not isinstance(today, str) or not _DATE_RE.fullmatch(today):
        raise ValueError('today must be YYYY-MM-DD, got %r' % (today,))
    first_seen = {s['song_key']: s['first_seen'] for s in _songs_of(prev_doc)
                  if isinstance(s.get('first_seen'), str) and _DATE_RE.fullmatch(s['first_seen'])}
    songs = [dict(s, first_seen=first_seen.get(s['song_key'], today), last_seen=today)
             for s in (batch_rank.get('songs') or []) if isinstance(s, Mapping)]
    doc: Dict[str, Any] = {
        'schema': TRENDING_SCHEMA, 'date': today,
        'generated_at': generated_at or iso_now(),
        'source': batch_rank.get('source') or SOURCE,
        'window_days': int(batch_rank.get('window_days') or WINDOW_DAYS),
        'stale': False,
        'accounts_ok': int(batch_rank.get('accounts_ok') or 0),
        'accounts_total': int(batch_rank.get('accounts_total') or 0),
        'accounts_ranked': int(batch_rank.get('accounts_ranked') or 0),
        'dropped': dict(batch_rank.get('dropped') or {}),
        'songs': songs,
    }
    for key, value in (('region', region), ('niche', niche)):
        if isinstance(value, str) and _TAG_RE.fullmatch(value.strip()):
            doc[key] = value.strip()
    return doc


# ── the published list: one writer ───────────────────────────────────────

def trends_dir() -> str:
    """``get_data_dir()/trends``: her desktop's watchlist, the daily job's
    state and session, the published songs and the song-language cache."""
    return os.path.join(get_data_dir(), 'trends')


def songs_dir() -> str:
    return os.path.join(trends_dir(), 'songs')


def _latest_path() -> str:
    return os.path.join(songs_dir(), 'latest.json')


def read_latest() -> Optional[dict]:
    """The published list, or None when there is none or it is damaged or
    refused by the contract (a list is never served unchecked)."""
    try:
        with open(_latest_path(), encoding='utf-8') as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get('schema') != TRENDING_SCHEMA:
        return None
    if validate_trending_songs(doc):
        return None
    return doc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def why_not_published(doc: Any) -> Optional[str]:
    """Why ``doc`` may not replace the published list, or None when it may:
    'invalid_list' (contracts.validate_trending_songs refuses it: malformed,
    a URL or session data anywhere), 'partial_run' (fewer than
    MIN_OK_PERCENT of the watched accounts answered) or 'empty_list' (no
    songs where the published list has some)."""
    if not isinstance(doc, Mapping) or doc.get('schema') != TRENDING_SCHEMA:
        return 'invalid_list'
    problems = validate_trending_songs(doc)
    if problems:
        logger.warning("trending songs: list refused: %s", '; '.join(problems[:5]))
        return 'invalid_list'
    ok, total = doc.get('accounts_ok'), doc.get('accounts_total')
    if _is_int(total) and total > 0 and not (_is_int(ok) and ok * 100 >= total * MIN_OK_PERCENT):
        return 'partial_run'
    if not doc['songs']:
        latest = read_latest()
        if latest and latest.get('songs'):
            return 'empty_list'
    return None


def publish(doc: Any, *, keep_days: int = KEEP_DAYS) -> bool:
    """Write ``doc`` as the day's file and as latest.json (each with
    core.file_cache.atomic_json_write), then drop day files older than
    ``keep_days`` before its date.  False, with nothing written, when
    why_not_published names a reason or the disk refuses."""
    with _write_lock:
        reason = why_not_published(doc)
        if reason:
            logger.warning("trending songs: list for %s not published (%s)",
                           doc.get('date') if isinstance(doc, Mapping) else '?', reason)
            return False
        folder = songs_dir()
        try:
            atomic_json_write(os.path.join(folder, doc['date'] + '.json'), doc)
            atomic_json_write(_latest_path(), doc)
        except OSError as e:
            logger.warning("trending songs: could not write the list: %s", e)
            return False
        _prune(folder, doc['date'], keep_days)
        logger.info("trending songs: published %d song(s) for %s",
                    len(doc['songs']), doc['date'])
        return True


def _prune(folder: str, today: str, keep_days: int) -> None:
    cutoff = date.fromisoformat(today) - timedelta(days=max(0, int(keep_days)))
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        match = _DAY_FILE_RE.fullmatch(name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(folder, name))
            except OSError as e:
                logger.debug("trending songs: could not prune %s: %s", name, e)


def mark_stale(reason: str) -> bool:
    """Keep the published list but say it is old and why ('challenge',
    'throttled', 'partial_run', ...).  False when nothing is published."""
    with _write_lock:
        latest = read_latest()
        if latest is None:
            return False
        latest['stale'] = True
        latest['stale_reason'] = str(reason)[:40]
        try:
            atomic_json_write(_latest_path(), latest)
        except OSError as e:
            logger.warning("trending songs: could not mark the list stale: %s", e)
            return False
        return True
