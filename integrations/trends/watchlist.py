"""integrations.trends.watchlist: the Instagram accounts her desktop's
trending-song run watches.

Owner ruling A17b: the owner supplies the handles, and her agent manages the
list (the reel_watchlist tool, integrations.reel_studio.tools: "track these
accounts", "stop tracking X", "which accounts do you track").  This module
is the one writer and the one reader of
``get_data_dir()/trends/watchlist.json`` (Gate 4); integrations.trends.
daily_job reads the accounts through ``read_watchlist`` and starts the
worker on them.

* A handle is what sources.HANDLE_RE takes (letters, digits, '.' and '_',
  at most 30: the rule the worker and the job already read with); one
  leading '@' is dropped.  Each is kept once whatever its case, in the order
  named, and at most sources.MAX_ACCOUNTS of them (what one run watches).
  Whatever was not taken is said back, with why.
* The niche (and the region, which nothing here changes) is a short tag,
  trending_songs.clean_tag: the rule merge_days copies it into the
  published list with.  Spaces in a niche she names become '_'.
* No credential ever passes through here.  The research account's username
  and password are asked for only on the credential consent card
  (daily_job.ask_for_account) and kept in the desktop's vault.

The file: ``{"region", "niche", "accounts": [handle | {"handle", "pk"}],
"updated_at"}``; an account's ``pk`` (its Instagram id, when known) is kept
across changes.  It is written atomically (core.file_cache.
atomic_json_write) under one lock.  A file that is missing or damaged reads
as an empty list, and the next change replaces a damaged one; a file that
is there but cannot be read (permissions, a folder in its place) is never
written over: the change raises WatchlistUnavailable.
"""
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.file_cache import atomic_json_write
from integrations.trends import sources as _sources
from integrations.trends import trending_songs as ts

logger = logging.getLogger(__name__)

WATCHLIST_FILE = 'watchlist.json'
#: The most accounts the list holds: what one run watches.
MAX_ACCOUNTS = _sources.MAX_ACCOUNTS
#: What a refused value is told back with.
NOT_A_HANDLE = "not an Instagram handle (letters, digits, '.' and '_', at most 30)"
NOT_A_NICHE = ("not a niche (a letter, then letters, digits, '_' or '-', at most 32; "
               "spaces become '_')")
#: The longest refused value said back.
_MAX_ECHO = 60

_lock = threading.Lock()


class WatchlistUnavailable(Exception):
    """The watchlist file is there but this computer cannot read or write it
    (permissions, a folder in its place, a full disk): nothing was changed."""


def watchlist_path() -> str:
    """``get_data_dir()/trends/watchlist.json``."""
    return os.path.join(ts.trends_dir(), WATCHLIST_FILE)


def _empty() -> Dict[str, Any]:
    return {'region': None, 'niche': None, 'accounts': []}


def _shape(data: Any) -> Dict[str, Any]:
    """The file's content as ``{'region', 'niche', 'accounts': [{'handle',
    'pk'}]}``: tags through trending_songs.clean_tag, accounts through
    sources.clean_accounts."""
    if not isinstance(data, dict):
        return _empty()
    return {'region': ts.clean_tag(data.get('region')), 'niche': ts.clean_tag(data.get('niche')),
            'accounts': _sources.clean_accounts(data.get('accounts'))}


def read_watchlist() -> Dict[str, Any]:
    """The accounts watched: ``{'region', 'niche', 'accounts': [{'handle',
    'pk'}]}``, each account an Instagram handle (sources.clean_accounts).
    No accounts when the file is missing, damaged or cannot be read.  Never
    raises."""
    try:
        with open(watchlist_path(), encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        logger.info("trending songs: the watchlist could not be read (%s); none is watched",
                    type(e).__name__)
        return _empty()
    return _shape(data)


def _load_for_change() -> Dict[str, Any]:
    """read_watchlist for a change: a damaged file is an empty list (the
    change replaces it), a file that cannot be read raises
    WatchlistUnavailable."""
    path = watchlist_path()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except ValueError:
        logger.warning("trending songs: the watchlist was damaged; this change replaces it")
        return _empty()
    except OSError as e:
        raise WatchlistUnavailable('the watchlist could not be read: %s' % type(e).__name__) \
            from e
    return _shape(data)


def _echo(value: Any) -> str:
    return ' '.join(str(value).split())[:_MAX_ECHO]


def parse_handles(value: Any) -> Tuple[List[str], List[Dict[str, str]]]:
    """``(handles, refused)`` from what her agent passes: a text of handles
    separated by commas, semicolons or new lines, or a list of them (each
    item one handle).  Not by spaces: a handle has none, so "glam by meena"
    is one piece and refused, never three accounts nobody named.  One
    leading '@' is dropped; each handle is kept once, case-blind, in order;
    ``refused`` is ``[{'value', 'why'}]`` for every piece that is not an
    Instagram handle (sources.HANDLE_RE)."""
    if isinstance(value, str):
        pieces = [p.strip() for p in re.split(r'[,;\r\n]+', value)]
    elif isinstance(value, (list, tuple)):
        pieces = [p.strip() if isinstance(p, str) else p for p in value]
    else:
        pieces = []
    handles: List[str] = []
    refused: List[Dict[str, str]] = []
    seen = set()
    for piece in pieces:
        if piece is None or piece == '':
            continue
        text = piece[1:] if isinstance(piece, str) and piece.startswith('@') else piece
        if not isinstance(text, str) or not _sources.HANDLE_RE.fullmatch(text):
            refused.append({'value': _echo(piece), 'why': NOT_A_HANDLE})
            continue
        if text.lower() not in seen:
            seen.add(text.lower())
            handles.append(text)
    return handles, refused


def _niche(value: Any) -> Optional[str]:
    """A niche she named as a tag (spaces become '_'), or None when it is
    not one."""
    if not isinstance(value, str):
        return None
    return ts.clean_tag(re.sub(r'\s+', '_', value.strip()))


def _entry(account: Dict[str, Optional[str]]) -> Any:
    """An account as the file keeps it: the handle, or ``{'handle', 'pk'}``
    when its id is known."""
    return {'handle': account['handle'], 'pk': account['pk']} if account['pk'] \
        else account['handle']


def update_watchlist(*, track: Any = (), untrack: Any = (),
                     niche: Optional[str] = None) -> Dict[str, Any]:
    """Change the watchlist in one write: stop watching ``untrack``, then
    start watching ``track`` (so one change can swap an account at the
    limit), then set ``niche`` (None leaves it as it is).  Handles are read
    by parse_handles.

    Returns ``{'tracked': [handles watched now], 'region', 'niche',
    'added', 'already_tracked', 'removed', 'not_tracked', 'over_limit'
    (handles past MAX_ACCOUNTS, not added), 'refused': [{'value', 'why'}],
    'changed', 'max_accounts'}``; handles already on the list are named as
    the list spells them.  Nothing is written when nothing changed.  Raises
    WatchlistUnavailable when the file cannot be read or written."""
    adds, refused = parse_handles(track)
    drops, refused_drops = parse_handles(untrack)
    refused += refused_drops
    with _lock:
        current = _load_for_change()
        accounts = list(current['accounts'])
        removed: List[str] = []
        not_tracked: List[str] = []
        for handle in drops:
            hit = next((a for a in accounts if a['handle'].lower() == handle.lower()), None)
            if hit is None:
                not_tracked.append(handle)
            else:
                accounts.remove(hit)
                removed.append(hit['handle'])
        added: List[str] = []
        already: List[str] = []
        over: List[str] = []
        for handle in adds:
            hit = next((a for a in accounts if a['handle'].lower() == handle.lower()), None)
            if hit is not None:
                already.append(hit['handle'])
            elif len(accounts) >= MAX_ACCOUNTS:
                over.append(handle)
            else:
                accounts.append({'handle': handle, 'pk': None})
                added.append(handle)
        new_niche = current['niche']
        if niche is not None and str(niche).strip():
            tag = _niche(niche)
            if tag is None:
                refused.append({'value': _echo(niche), 'why': NOT_A_NICHE})
            else:
                new_niche = tag
        changed = bool(removed or added or new_niche != current['niche'])
        if changed:
            doc = {'region': current['region'], 'niche': new_niche,
                   'accounts': [_entry(a) for a in accounts], 'updated_at': _sources.iso_now()}
            try:
                atomic_json_write(watchlist_path(), doc)
            except OSError as e:
                raise WatchlistUnavailable('the watchlist could not be written: %s'
                                           % type(e).__name__) from e
            logger.info("trending songs: watchlist changed (+%d, -%d), %d account(s) watched",
                        len(added), len(removed), len(accounts))
    return {'tracked': [a['handle'] for a in accounts], 'region': current['region'],
            'niche': new_niche, 'added': added, 'already_tracked': already,
            'removed': removed, 'not_tracked': not_tracked, 'over_limit': over,
            'refused': refused, 'changed': changed, 'max_accounts': MAX_ACCOUNTS}
