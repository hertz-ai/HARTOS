"""igkit.swipe: rank reels by how far each beat its own account, name the
hook formula each used, and score the songs they used.

Derived from instagram-agent-skill @d03c56b skills/ig-viral/swipe.py,
Copyright (c) 2026 Jake Schincariol, MIT License (see LICENSE.txt in this
package).  Modified for HARTOS Reel Studio: rows are dicts (no TSV, no
markdown, no printing), the hook scorer and formulas come in through a
normal import instead of a ``sys.path`` hack, rows whose song is muted are
dropped, ``account_median`` and ``outlier`` are their own functions, and
songs are grouped and scored (``top_songs``).

The one idea, in upstream's words: raw views are not evidence.  A
2,000,000-follower account doing 400,000 views had a quiet day; a
4,000-follower account doing 400,000 views found something.  Everything
here ranks on the multiple over the account's own baseline.

This is the ONE implementation of that maths (Gate 4): the central trends
job scores songs through rank_reels rather than keeping a copy.  Song
score = sum over distinct accounts of log1p(that account's best multiple
for the song), each row optionally weighted (``weight``, e.g. a recency
decay the caller computes).
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from integrations.reel_studio.igkit import words
from integrations.reel_studio.igkit.hooks import classify_hook, rate_hook

#: Most recent reels an account's baseline is taken from, and the fewest
#: that make a baseline (goals-trends report 2.3).
MEDIAN_WINDOW = 12
MEDIAN_MIN = 6

#: A count written as text: digits with thousands separators in either
#: grouping ("412,000", "1,50,000"), an optional decimal part, and an
#: optional multiplier ("4.5k", "1.2M", "1.5 lakh", "2 crore").
_COUNT_TEXT = re.compile(r'(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?\s*([a-z]+)?', re.IGNORECASE)
_MULTIPLIERS = {'k': 10 ** 3, 'm': 10 ** 6, 'b': 10 ** 9, 'lakh': 10 ** 5, 'lakhs': 10 ** 5,
                'lac': 10 ** 5, 'lacs': 10 ** 5, 'cr': 10 ** 7, 'crore': 10 ** 7,
                'crores': 10 ** 7}


def _count_text(text: str):
    """The count ``text`` writes, or None when it is not one count."""
    m = _COUNT_TEXT.fullmatch(text.strip())
    if not m:
        return None
    whole, frac, unit = m.group(1).replace(',', ''), m.group(2), m.group(3)
    if unit:
        scale = _MULTIPLIERS.get(unit.lower())
        return None if scale is None else int(round(float(whole + (frac or '')) * scale))
    return float(whole + frac) if frac else int(whole)


def _amount(value) -> Optional[float]:
    """A count or size: a non-negative int or float, or one count written as
    text ("412,000", "1,50,000", "12.5", "4.5k", "1.2M", "1.5 lakh"); None
    for anything else (bools, negatives, words, "1.2.3", "12 views")."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return value if math.isfinite(value) and value >= 0 else None
    if isinstance(value, str):
        return _count_text(value)
    return None


def account_median(plays: Iterable[Any], *, window: int = MEDIAN_WINDOW,
                   min_n: int = MEDIAN_MIN) -> Optional[float]:
    """An account's typical views: the median of its newest ``window``
    reels (``plays`` newest first), or None with fewer than ``min_n``.

    Values that are not counts (None, bools, negatives, words) are skipped.
    """
    counts = [c for c in (_amount(p) for p in (plays or ())) if c is not None][:window]
    if len(counts) < min_n:
        return None
    return float(statistics.median(counts))


def outlier(views: Any, baseline: Any) -> Optional[float]:
    """``views / baseline`` to 2 decimals; None without a positive baseline."""
    v, b = _amount(views), _amount(baseline)
    if v is None or not b:
        return None
    return round(v / b, 2)


def _weight(value) -> float:
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
            math.isfinite(value) and value >= 0):
        raise ValueError('row weight must be a number >= 0, got %r' % (value,))
    return float(value)


def _first_start(value) -> Optional[int]:
    """A song start in ms from an int or a list of highlight starts."""
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if _amount(v) is not None and not isinstance(v, str)),
                     None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 else None


def _median(items: Sequence[Mapping], key: str) -> Optional[float]:
    vals = [i[key] for i in items if i.get(key) is not None]
    return round(statistics.median(vals), 1) if vals else None


def _first_text(rows: Sequence[Mapping], key: str) -> Optional[str]:
    return next((r[key] for r in rows if isinstance(r.get(key), str) and r[key].strip()), None)


def _top_songs(ranked: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in ranked:                      # ranked: best multiple first
        sid = r.get('song_id')
        if sid is None or isinstance(sid, bool) or not str(sid).strip():
            continue
        groups.setdefault(str(sid), []).append(r)
    songs = []
    for sid, rows in groups.items():
        best: Dict[str, float] = {}
        for r in rows:
            gain = _weight(r.get('weight')) * math.log1p(r['outlier']) if r['outlier'] else 0.0
            acct = str(r.get('account') or '')
            best[acct] = max(best.get(acct, 0.0), gain)
        multiples = [r['outlier'] for r in rows if r['outlier'] is not None]
        start = next((s for s in (_first_start(r.get('highlight_start_ms')) for r in rows)
                      if s is not None), 0)
        songs.append({
            'song_id': sid,
            'title': _first_text(rows, 'song_title'),
            'artist': _first_text(rows, 'artist'),
            'lang': _first_text(rows, 'lang'),
            'n_reels': len(rows),
            'accounts': len(best),
            'median_outlier': round(statistics.median(multiples), 2) if multiples else None,
            'max_outlier': max(multiples) if multiples else None,
            # The start the best-performing reel used (instagrapi's rule:
            # the first highlight start), else 0.
            'best_start_ms': start,
            'score': round(sum(best.values()), 3),
        })
    songs.sort(key=lambda s: (-s['score'], -s['accounts'], -(s['max_outlier'] or 0),
                              s['song_id']))
    return songs


def rank_reels(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Rank collected reels by their multiple over their account's baseline.

    Each row is a mapping with ``views`` and, when known, ``account``,
    ``followers``, ``median`` (the account's typical views, the better
    baseline; see account_median), ``hook``, ``song_id``, ``song_title``,
    ``artist``, ``lang``, ``should_mute``, ``highlight_start_ms`` (an int or a
    list) and ``weight`` (default 1.0).  Counts may be ints, or text such
    as "412,000" or "1.2M"; text that is not one count is no count.
    Rows whose song is muted, and rows with no views, are
    dropped and counted.  The caller's rows are not changed.

    Returns ``{'baseline': 'account median'|'follower count', 'n',
    'accounts', 'reels', 'top_formulas': [[name, count]], 'top_hook_score',
    'bottom_hook_score', 'top_words', 'bottom_words', 'unclassified',
    'dropped': {'should_mute', 'no_views'}, 'top_songs'}``.  Each reel gains
    ``baseline``, ``outlier``, ``formula_id``, ``formula``, ``words`` and
    ``hook_score`` (research only; never shown to her).  ``top_songs``
    entries are ``{'song_id', 'title', 'artist', 'lang', 'n_reels',
    'accounts', 'median_outlier', 'max_outlier', 'best_start_ms',
    'score'}``, best first.  Raises TypeError for a row that is not a
    mapping and ValueError for a negative or non-numeric weight.
    """
    reels: List[Dict[str, Any]] = []
    dropped = {'should_mute': 0, 'no_views': 0}
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            raise TypeError('a reel row must be a mapping, got %r' % (type(raw).__name__,))
        r = dict(raw)
        if r.get('should_mute'):
            dropped['should_mute'] += 1
            continue
        views = _amount(r.get('views'))
        if not views:
            dropped['no_views'] += 1
            continue
        r['views'] = views
        for key in ('followers', 'median'):
            r[key] = _amount(r.get(key))
        _weight(r.get('weight'))
        base = r['median'] or r['followers'] or 0
        r['baseline'] = base
        r['outlier'] = outlier(views, base)
        hook = r.get('hook') if isinstance(r.get('hook'), str) and r['hook'].strip() else None
        r['formula_id'], r['formula'] = classify_hook(hook) if hook else (None, 'unclassified')
        r['words'] = len(words(hook)) if hook else None
        r['hook_score'] = rate_hook(hook)['score'] if hook else None
        reels.append(r)

    ranked = sorted(reels, key=lambda x: -(x['outlier'] or 0))
    third = max(1, len(ranked) // 3)
    top, bottom = ranked[:third], ranked[-third:]
    counts: Dict[str, int] = {}
    for r in top:
        counts[r['formula']] = counts.get(r['formula'], 0) + 1
    return {
        'baseline': 'account median' if any(r.get('median') for r in ranked) else 'follower count',
        'n': len(ranked),
        'accounts': len({r.get('account', '') for r in ranked}),
        'reels': ranked,
        'top_formulas': [[name, n] for name, n in sorted(counts.items(), key=lambda kv: -kv[1])],
        'top_hook_score': _median(top, 'hook_score'),
        'bottom_hook_score': _median(bottom, 'hook_score'),
        'top_words': _median(top, 'words'),
        'bottom_words': _median(bottom, 'words'),
        'unclassified': sum(1 for r in ranked if r['formula'] == 'unclassified'),
        'dropped': dropped,
        'top_songs': _top_songs(ranked),
    }
