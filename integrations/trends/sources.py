"""integrations.trends.sources: where central's trending songs come from.

Standard library only.  Two processes use this file: HARTOS (central_job,
trending_songs and the tests) imports it as integrations.trends.sources,
and the trends worker (ig_worker.py, central's separate interpreter run
with -I, where nothing from HARTOS is importable) loads this same file by
path.  So it holds what both sides must agree on, once:

* ``TrendSource``: what the run loop asks of a source (instagrapi today, a
  paid scraping service later), and ``TrendSourceError`` with its code;
* ``music_of``: the one reader of a reel's music.  The worker uses it to
  know which songs need a track lookup; trending_songs.normalize_reel uses
  it to key and name each song;
* ``slim_reel`` / ``slim_track``: what a reel or a track is cut down to
  before it leaves the worker: only what music_of and normalize_reel read,
  never a URL, a manifest, a caption or a profile;
* ``collect``: the run over the watched accounts, with its stop rules and
  its request budget (goals-trends report 2.5).

Shapes are instagrapi 3.0.24's (types.py Media, ClipsMetadata,
ClipsOriginalSoundInfo, ClipsMusicAttributionInfo, Track), read from its
source, as dicts or as pydantic models (anything with model_dump).
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol

#: The worker's output file, read by central_job.
BATCH_SCHEMA = 'hart.trends.batch.v1'

#: Most accounts one run watches, the request budget of one run (each
#: private API call counts, the login's included), and the reels asked for
#: per account: one page, the Instagram app's own page size.
MAX_ACCOUNTS = 25
DEFAULT_BUDGET = 60
PAGE_SIZE = 12

#: Codes that end the whole run: the research account needs a person
#: (challenge, login) or Instagram asked it to slow down (throttled,
#: feedback).  Going on would make either worse.
STOP_CODES = ('challenge', 'login', 'throttled', 'feedback')
#: Codes that skip one account (or one track) and go on.
SKIP_CODES = ('not_found', 'transport', 'unknown')
ERROR_CODES = STOP_CODES + SKIP_CODES

#: This many transport failures in a row mean the network is down, not one
#: account, so the run stops instead of spending each remaining account's
#: retries on it.
TRANSPORT_STOP_AFTER = 3

#: An Instagram handle: letters, digits, '.' and '_', at most 30.
HANDLE_RE = re.compile(r'[A-Za-z0-9._]{1,30}')

_ID_RE = re.compile(r'[0-9A-Za-z_-]{1,64}')
_MAX_TEXT = 200
_MAX_HIGHLIGHTS = 10

#: What slim_reel keeps of each part of a reel: exactly what music_of and
#: trending_songs.normalize_reel read.
_REEL_KEYS = ('pk', 'id', 'code', 'taken_at', 'play_count', 'view_count',
              'video_play_count', 'video_view_count')
_USER_KEYS = ('pk', 'username')
_ATTRIBUTION_KEYS = ('artist_name', 'song_name', 'uses_original_audio',
                     'should_mute_audio', 'audio_id')
_META_KEYS = ('audio_type', 'music_canonical_id')
_ASSET_KEYS = ('id', 'audio_cluster_id', 'audio_asset_id', 'music_canonical_id', 'title',
               'display_artist', 'highlight_start_times_in_ms', 'duration_in_ms',
               'is_explicit')
_CONSUMPTION_KEYS = ('should_mute_audio',)
_ORIGINAL_KEYS = ('audio_asset_id', 'music_canonical_id', 'original_audio_title',
                  'duration_in_ms', 'is_explicit', 'should_mute_audio')
#: instagrapi's Track fields a song needs; its URLs, manifest, cover art and
#: territory data stay behind.
TRACK_KEYS = ('id', 'title', 'subtitle', 'display_artist', 'audio_cluster_id',
              'music_canonical_id', 'highlight_start_times_in_ms', 'is_explicit',
              'audio_asset_id', 'duration_in_ms')


class TrendSourceError(Exception):
    """A source call that did not answer.  ``code`` is one of ERROR_CODES
    (anything else reads as 'unknown'); ``pk`` is the account's id when the
    source learned it before failing, so it is cached all the same."""

    def __init__(self, code: str, detail: str = '', *, pk: Optional[str] = None) -> None:
        code = code if code in ERROR_CODES else 'unknown'
        super().__init__('%s: %s' % (code, detail) if detail else code)
        self.code = code
        self.detail = detail
        self.pk = pk


class TrendSource(Protocol):
    """What collect asks of a source.  Every method raises TrendSourceError
    when the source did not answer."""

    def fetch_reels(self, handle: str, pk_hint: Optional[str]) -> dict:
        """``{'pk': <the account's id>, 'reels': [<reel dict or model>]}``:
        the account's newest page of reels.  ``pk_hint`` is its id when it
        is already known, which saves the lookup."""

    def lookup_track(self, song_key: str) -> Optional[dict]:
        """The track a ``canonical:<id>`` song key names (a dict or model in
        instagrapi's Track shape), or None when the source cannot look it
        up."""

    def requests_used(self) -> int:
        """Requests this source has made so far in the run."""


# ── small readers ─────────────────────────────────────────────────────────

def as_dict(value: Any) -> Optional[dict]:
    """``value`` as a plain dict: a mapping, or a pydantic model dumped in
    JSON mode (so datetimes come back as ISO text).  None otherwise."""
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, 'model_dump', None)
    if not callable(dump):
        return None
    try:
        data = dump(mode='json')
    except TypeError:
        try:
            data = dump()
        except Exception:
            return None
    except Exception:
        return None
    return dict(data) if isinstance(data, Mapping) else None


def clean_id(value: Any) -> Optional[str]:
    """An Instagram id as text: a non-negative int, or text of letters,
    digits, '_' and '-' (at most 64).  None for anything else, bools
    included."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str):
        value = value.strip()
        return value if _ID_RE.fullmatch(value) else None
    return None


def _text(value: Any) -> str:
    return ' '.join(value.split())[:_MAX_TEXT] if isinstance(value, str) else ''


def _count(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _ms_list(value: Any) -> List[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return [c for c in (_count(v) for v in value) if c is not None][:_MAX_HIGHLIGHTS]


def _pick(data: Mapping, keys: Iterable[str]) -> dict:
    return {k: data[k] for k in keys if k in data}


# ── a reel's music ────────────────────────────────────────────────────────

def music_of(reel: Any) -> Dict[str, Any]:
    """The song a reel uses, as far as the reel itself says.

    The song key, in order: the licensed song's audio cluster
    (``clips_metadata.music_info.music_asset_info.audio_cluster_id``,
    'cluster:<id>'); an original sound's audio asset
    (``clips_metadata.original_sound_info.audio_asset_id``, 'original:<id>');
    the canonical id alone ('canonical:<id>', ``needs_track`` True: the
    track lookup names it); the attribution's audio id alone
    ('audio:<id>').  None when the reel names no song.

    Returns ``{'song_key', 'kind' ('licensed'|'original'|'canonical'|
    'attribution'|None), 'audio_cluster_id', 'audio_asset_id', 'asset_ids',
    'music_canonical_id', 'title', 'artist', 'highlight_start_ms',
    'duration_ms', 'needs_track', 'explicit', 'uses_original_audio',
    'mute_flags'}``.  The last three are the raw values as sent; the HARTOS
    side reads them with core.config_cache.flag_value (this file cannot
    import it).
    """
    reel = as_dict(reel) or {}
    meta = as_dict(reel.get('clips_metadata')) or {}
    info = as_dict(meta.get('music_info')) or {}
    asset = as_dict(info.get('music_asset_info')) or {}
    consumption = as_dict(info.get('music_consumption_info')) or {}
    original = as_dict(meta.get('original_sound_info')) or {}
    attribution = as_dict(reel.get('clips_music_attribution_info')) or {}
    canonical = (clean_id(meta.get('music_canonical_id'))
                 or clean_id(asset.get('music_canonical_id'))
                 or clean_id(original.get('music_canonical_id')))
    out: Dict[str, Any] = {
        'song_key': None, 'kind': None, 'audio_cluster_id': None, 'audio_asset_id': None,
        'asset_ids': (), 'music_canonical_id': canonical,
        'title': _text(attribution.get('song_name')),
        'artist': _text(attribution.get('artist_name')),
        'highlight_start_ms': [], 'duration_ms': None, 'needs_track': False,
        'explicit': None, 'uses_original_audio': attribution.get('uses_original_audio'),
        'mute_flags': (attribution.get('should_mute_audio'),
                       original.get('should_mute_audio'),
                       consumption.get('should_mute_audio')),
    }
    cluster = clean_id(asset.get('audio_cluster_id'))
    if cluster:
        ids = tuple(dict.fromkeys(i for i in (clean_id(asset.get('audio_asset_id')),
                                              clean_id(asset.get('id'))) if i))
        out.update(song_key='cluster:' + cluster, kind='licensed', audio_cluster_id=cluster,
                   audio_asset_id=ids[0] if ids else None, asset_ids=ids,
                   title=_text(asset.get('title')) or out['title'],
                   artist=_text(asset.get('display_artist')) or out['artist'],
                   highlight_start_ms=_ms_list(asset.get('highlight_start_times_in_ms')),
                   duration_ms=_count(asset.get('duration_in_ms')),
                   explicit=asset.get('is_explicit'))
        return out
    original_id = clean_id(original.get('audio_asset_id'))
    if original_id:
        owner = as_dict(original.get('ig_artist')) or {}
        out.update(song_key='original:' + original_id, kind='original',
                   audio_asset_id=original_id, asset_ids=(original_id,),
                   title=_text(original.get('original_audio_title')) or out['title'],
                   artist=_text(owner.get('username')) or out['artist'],
                   duration_ms=_count(original.get('duration_in_ms')),
                   explicit=original.get('is_explicit'))
        return out
    if canonical:
        out.update(song_key='canonical:' + canonical, kind='canonical', needs_track=True)
        return out
    audio = clean_id(attribution.get('audio_id'))
    if audio:
        out.update(song_key='audio:' + audio, kind='attribution', audio_asset_id=audio,
                   asset_ids=(audio,))
    return out


def slim_reel(raw: Any) -> dict:
    """``raw`` cut down to what music_of and normalize_reel read: ids, the
    counts, the owner's pk and handle, and the music parts.  No caption,
    video, image, URL or manifest survives.  {} for anything that is not a
    reel-like mapping or model."""
    data = as_dict(raw)
    if data is None:
        return {}
    out = _pick(data, _REEL_KEYS)
    if isinstance(out.get('taken_at'), datetime):
        out['taken_at'] = out['taken_at'].isoformat()
    user = as_dict(data.get('user'))
    if user is not None:
        out['user'] = _pick(user, _USER_KEYS)
    if 'clips_music_attribution_info' in data:
        attribution = as_dict(data['clips_music_attribution_info'])
        out['clips_music_attribution_info'] = (None if attribution is None
                                               else _pick(attribution, _ATTRIBUTION_KEYS))
    if 'clips_metadata' in data:
        meta = as_dict(data['clips_metadata'])
        out['clips_metadata'] = None if meta is None else _slim_meta(meta)
    return out


def _slim_meta(meta: Mapping) -> dict:
    out = _pick(meta, _META_KEYS)
    if 'music_info' in meta:
        info = as_dict(meta['music_info'])
        if info is None:
            out['music_info'] = None
        else:
            slim: Dict[str, Any] = {}
            if 'music_asset_info' in info:
                asset = as_dict(info['music_asset_info'])
                slim['music_asset_info'] = None if asset is None else _pick(asset, _ASSET_KEYS)
            if 'music_consumption_info' in info:
                consumption = as_dict(info['music_consumption_info'])
                slim['music_consumption_info'] = (None if consumption is None
                                                  else _pick(consumption, _CONSUMPTION_KEYS))
            out['music_info'] = slim
    if 'original_sound_info' in meta:
        original = as_dict(meta['original_sound_info'])
        if original is None:
            out['original_sound_info'] = None
        else:
            slim = _pick(original, _ORIGINAL_KEYS)
            owner = as_dict(original.get('ig_artist'))
            if owner is not None:
                slim['ig_artist'] = _pick(owner, ('username',))
            out['original_sound_info'] = slim
    return out


def slim_track(raw: Any) -> Optional[dict]:
    """A looked-up track cut down to TRACK_KEYS (instagrapi's field names);
    None for anything that is not one."""
    data = as_dict(raw)
    if not data:
        return None
    return _pick(data, TRACK_KEYS)


# ── the run ───────────────────────────────────────────────────────────────

def clean_accounts(accounts: Any) -> List[Dict[str, Optional[str]]]:
    """``[{'handle', 'pk'}]`` from a watchlist's entries (a handle, or
    ``{'handle', 'pk'}``): valid Instagram handles only (HANDLE_RE), each
    once (case-folded), at most MAX_ACCOUNTS, in order."""
    out: List[Dict[str, Optional[str]]] = []
    seen = set()
    if not isinstance(accounts, (list, tuple)):
        return out
    for entry in accounts:
        if isinstance(entry, str):
            handle, pk = entry, None
        elif isinstance(entry, Mapping):
            handle, pk = entry.get('handle'), entry.get('pk')
        else:
            continue
        handle = handle.strip() if isinstance(handle, str) else ''
        if not HANDLE_RE.fullmatch(handle) or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        out.append({'handle': handle, 'pk': clean_id(pk)})
        if len(out) >= MAX_ACCOUNTS:
            break
    return out


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _used(source: Any) -> int:
    try:
        return int(source.requests_used())
    except Exception:
        return 0


def _row(handle: str, pk: Optional[str], error: Optional[str], detail: Optional[str] = None,
         reels: Optional[list] = None) -> dict:
    return {'handle': handle, 'pk': pk, 'ok': error is None, 'error': error,
            'error_detail': detail, 'reels': list(reels or [])}


def failed_batch(code: str, detail: str, accounts: Any, *, requests: int = 0,
                 budget: int = DEFAULT_BUDGET) -> dict:
    """The batch of a run that stopped before its first account (no
    instagrapi, no credentials, a refused login): every account 'not_run'."""
    rows = [_row(a['handle'], a['pk'], 'not_run') for a in clean_accounts(accounts)]
    return _batch(rows, {}, code, detail, requests=requests, budget=budget,
                  budget_exhausted=False, deadline_reached=False, source=None)


def _batch(rows: List[dict], tracks: dict, error: Optional[str], detail: Optional[str], *,
           requests: int, budget: int, budget_exhausted: bool, deadline_reached: bool,
           source: Optional[str]) -> dict:
    now = iso_now()
    return {'schema': BATCH_SCHEMA, 'source': source, 'started_at': now, 'finished_at': now,
            'error': error, 'error_detail': detail, 'requests': requests, 'budget': budget,
            'budget_exhausted': budget_exhausted, 'deadline_reached': deadline_reached,
            'accounts_total': len(rows), 'accounts_ok': sum(1 for r in rows if r['ok']),
            'accounts': rows, 'tracks': tracks}


def collect(source: TrendSource, accounts: Any, *, budget: int = DEFAULT_BUDGET,
            known_song_keys: Iterable[str] = (), deadline: Optional[float] = None,
            clock: Callable[[], float] = time.monotonic) -> dict:
    """One run: each account's newest page of reels, then a track lookup
    for each song known only by its canonical id that is not already in
    ``known_song_keys``, each looked up once.

    Before every call the run checks ``deadline`` (on ``clock``) and the
    request ``budget`` (``source.requests_used()``); past either, the
    accounts not reached are marked 'deadline' or 'budget' and no more
    calls are made.  A STOP_CODES error ends the run there (the accounts
    after it are 'not_run', no tracks are looked up); a SKIP_CODES error
    skips that account or track, except that TRANSPORT_STOP_AFTER transport
    failures in a row end the run as 'transport'.  Anything else a source
    raises skips that account as 'unknown'.  Reels and tracks are slimmed
    here (slim_reel, slim_track), so nothing else leaves the worker.

    Returns the BATCH_SCHEMA batch: ``{'schema', 'source', 'started_at',
    'finished_at', 'error', 'error_detail', 'requests', 'budget',
    'budget_exhausted', 'deadline_reached', 'accounts_total',
    'accounts_ok', 'accounts': [{'handle', 'pk', 'ok', 'error',
    'error_detail', 'reels'}], 'tracks': {song_key: track}}``.
    """
    started = iso_now()
    todo = clean_accounts(accounts)
    rows: List[dict] = []
    error: Optional[str] = None
    detail: Optional[str] = None
    halted: Optional[str] = None
    in_a_row = 0

    def _halt() -> Optional[str]:
        if deadline is not None and clock() >= deadline:
            return 'deadline'
        if _used(source) >= budget:
            return 'budget'
        return None

    for acct in todo:
        handle = acct['handle']
        if error is not None or halted is not None:
            rows.append(_row(handle, acct['pk'], halted or 'not_run'))
            continue
        halted = _halt()
        if halted is not None:
            rows.append(_row(handle, acct['pk'], halted))
            continue
        try:
            got = source.fetch_reels(handle, acct['pk'])
            if not isinstance(got, Mapping):
                raise TrendSourceError('unknown', 'the source answered %s'
                                       % type(got).__name__)
        except TrendSourceError as e:
            code, why, pk = e.code, e.detail, e.pk or acct['pk']
        except Exception as e:
            code, why, pk = 'unknown', type(e).__name__, acct['pk']
        else:
            reels = [r for r in (slim_reel(raw) for raw in (got.get('reels') or [])) if r]
            rows.append(_row(handle, clean_id(got.get('pk')) or acct['pk'], None, reels=reels))
            in_a_row = 0
            continue
        rows.append(_row(handle, clean_id(pk), code, why))
        if code in STOP_CODES:
            error, detail = code, why
        elif code == 'transport':
            in_a_row += 1
            if in_a_row >= TRANSPORT_STOP_AFTER:
                error, detail = 'transport', '%d transport failures in a row' % in_a_row
        else:
            in_a_row = 0

    tracks: Dict[str, dict] = {}
    if error is None and halted is None:
        known = {k for k in known_song_keys if isinstance(k, str)}
        wanted: List[str] = []
        for row in rows:
            for reel in row['reels']:
                music = music_of(reel)
                key = music['song_key']
                if music['needs_track'] and key not in known and key not in wanted:
                    wanted.append(key)
        for key in wanted:
            halted = _halt()
            if halted is not None:
                break
            try:
                track = slim_track(source.lookup_track(key))
            except TrendSourceError as e:
                if e.code in STOP_CODES:
                    error, detail = e.code, e.detail
                    break
                if e.code == 'transport':
                    in_a_row += 1
                    if in_a_row >= TRANSPORT_STOP_AFTER:
                        error, detail = 'transport', '%d transport failures in a row' % in_a_row
                        break
                continue
            except Exception:
                continue
            in_a_row = 0
            if track:
                tracks[key] = track

    batch = _batch(rows, tracks, error, detail, requests=_used(source), budget=budget,
                   budget_exhausted=halted == 'budget', deadline_reached=halted == 'deadline',
                   source=getattr(source, 'name', None))
    batch['started_at'] = started
    return batch
