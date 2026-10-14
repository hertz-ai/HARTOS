"""Builders for the trending-songs tests: reels from reels.json, varied.

Imported as ``tests.fixtures.trends.build``.  Nothing here touches the
network or the data root; it only shapes dicts the way central's worker
(integrations/trends/ig_worker.py) hands them back.
"""
import copy
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_SCHEMA = 'hart.trends.batch.v1'
DAY = 86400.0


def load() -> dict:
    """reels.json, freshly parsed (callers may change what they get)."""
    with open(os.path.join(HERE, 'reels.json'), encoding='utf-8') as fh:
        data = json.load(fh)
    data.pop('_about', None)
    return data


def user_pk(handle: str) -> int:
    """A stable invented user pk for ``handle``."""
    return 9000000000 + int(hashlib.sha256(handle.encode()).hexdigest()[:8], 16) % 10 ** 9


def reel(kind='licensed', *, pk, handle, plays, taken_at, cluster=None, title=None,
         artist=None, highlights=None, muted=None):
    """One raw reel of ``kind`` (a key of reels.json) with its identity,
    owner, plays and time replaced; for a licensed reel ``cluster``,
    ``title``, ``artist`` and ``highlights`` replace its song."""
    r = copy.deepcopy(load()[kind])
    upk = user_pk(handle)
    r.update({'pk': pk, 'id': '%d_%d' % (pk, upk), 'code': 'C%d' % pk,
              'taken_at': taken_at, 'play_count': plays,
              'user': {'pk': upk, 'username': handle}})
    meta = r.get('clips_metadata') or {}
    asset = ((meta.get('music_info') or {}).get('music_asset_info'))
    attr = r.get('clips_music_attribution_info')
    if asset is not None:
        if cluster is not None:
            asset['audio_cluster_id'] = cluster
            asset['id'] = str(cluster)
            if attr is not None:
                attr['audio_id'] = str(cluster)
        if title is not None:
            asset['title'] = title
            if attr is not None:
                attr['song_name'] = title
        if artist is not None:
            asset['display_artist'] = artist
            if attr is not None:
                attr['artist_name'] = artist
        if highlights is not None:
            asset['highlight_start_times_in_ms'] = list(highlights)
    if muted is not None and attr is not None:
        attr['should_mute_audio'] = muted
    return r


def account(handle, plays, *, now, songs=None, first_pk, step_s=DAY / 2, **song_kw):
    """Reels of one account, newest first: ``plays[i]`` views, taken
    ``i * step_s`` before ``now``.  ``songs[i]`` is a cluster id (a licensed
    reel), a dict of reel() keywords, or None (a reel with no music)."""
    out = []
    for i, p in enumerate(plays):
        song = songs[i] if songs is not None else None
        kw = dict(pk=first_pk + i, handle=handle, plays=p, taken_at=int(now - i * step_s))
        if song is None:
            out.append(reel('no_music', **kw))
        elif isinstance(song, dict):
            out.append(reel(**dict(kw, **song)))
        else:
            out.append(reel('licensed', cluster=song, **dict(kw, **song_kw)))
    return out


def batch(accounts, *, failed=(), error=None, tracks=None, requests=None):
    """A worker batch (hart.trends.batch.v1): ``accounts`` maps a handle to
    its raw reels; ``failed`` maps a handle to its error code."""
    rows = [{'handle': h, 'pk': str(user_pk(h)), 'ok': True, 'error': None,
             'reels': list(reels)} for h, reels in accounts.items()]
    rows += [{'handle': h, 'pk': None, 'ok': False, 'error': code, 'reels': []}
             for h, code in dict(failed).items()]
    return {'schema': BATCH_SCHEMA, 'started_at': '2026-10-13T03:00:00Z',
            'finished_at': '2026-10-13T03:06:00Z', 'error': error,
            'requests': len(rows) if requests is None else requests, 'budget': 60,
            'budget_exhausted': False, 'accounts_total': len(rows),
            'accounts_ok': len(accounts), 'accounts': rows, 'tracks': dict(tracks or {})}
