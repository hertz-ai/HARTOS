"""core.chat_attachments -- a chat turn's file references in, its reel out.

Reel Studio (plan WS-8a).  The phone uploads clips, a logo and song audio
into the user file store (core.user_file_store) over its device link, and a
device sends chat attachments into the same store over HTTP (Nunba's
desktop/file_sync); a /chat turn carries only references to them (plan
4.2(e)), in the ONE attachments shape the chat mirror documents
(ConversationEntry.attachments, U9; amendment A9 a)::

    "attachments": [{"file_id": "<64-hex xid>", "sha256": "<the same>",
                     "name": ..., "mime": ..., "size": ...,
                     "purpose": "<one of the store's INBOUND_PURPOSES>",  # optional
                     "captured_at": ..., "duration_ms": ...}]    # optional

``file_id`` is the store xid, which is the file's sha256.  The reel the turn
makes goes into the same store's outbox, and the reply that turn ends with
carries it as ``media`` (plan 4.2(f)) and its mirror row in the same shape
(media_to_mirror): what goes out is what comes in.

ONE rule judges a row, in and out: validate_attachment.  parse_inbound keeps
exactly the rows it accepts, media_to_mirror writes only rows it accepts,
integrations.reel_studio.contracts.validate_attachment IS this function, and
Nunba's turn_attachments forwards only the rows it accepts.  A row is not
half-read: one the rule refuses for any field (a name such as
'../evil.mp4', a MIME type its purpose does not take, no size) binds
nothing.  A row it accepts binds by its file_id and purpose, and from then
on the store's own record of the file counts, not the row's name, mime or
size.

This module owns NO file state.  It judges and parses a turn's
``attachments`` (pure) and delegates every read and write to the store, which
is the one place files, turn bindings, consumption and delivery are recorded:

    validate_attachment(row)      what is wrong with one row (pure)
    parse_inbound(raw)            the turn's well-formed references (pure)
    bind_inbound(...)             tie them to (user, prompt, request)
    latest_inbound(...)           what a reel turn may use
    take_outbound(...)            this turn's reel media, taken once
    media_to_mirror(media)        the chat mirror's attachment rows (pure)

The store-facing calls never raise: a /chat turn must not fail because a
reference was bad or the disk was unhappy.  A reference that does not resolve
to this user's inbox is dropped and logged.

LAYERING: core/ may not import integrations/, so the row rule lives here and
the facts it is built from (the inbound purposes and their MIME types, the
offer's meta keys, the xid, file-name and capture-time rules) are the
store's; integrations.reel_studio.contracts imports the rule from here and
the facts from the store (amendment A9 d): there is one copy, not two held
equal.

A row's ``purpose`` matters: the same bytes can be held for two purposes
(her clip is also a chat attachment), and a row binds only the purpose it
names.  A row naming none -- the documented U9 shape, which is a chat
attachment's -- binds her chat attachment when the file is one, else the
file's claim, and never moves a claim that is bound to another turn: her
clip stays on her reel turn (core.user_file_store.UserFileStore.bind).  A
client that sends the same file in a later turn names its purpose to move
it there (Nunba's /api/chat-sync/files/push answers with it).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# The store's own facts and rules (an xid is 64 lowercase hex, a bare file
# name, a capture time in epoch ms, the purposes and their MIME types), asked
# here rather than copied, so a row is well-formed exactly when the store
# would accept what it names.
from core.user_file_store import (INBOUND_META_KEYS, INBOUND_MIMES, INBOUND_PURPOSES,
                                  REEL_SOURCE, get_user_file_store, is_capture_ms,
                                  is_file_name, is_xid)

logger = logging.getLogger(__name__)

#: At most this many attachments are read from one turn (plan 4.2(e); Nunba's
#: turn_attachments sends no more).
MAX_ATTACHMENTS = 10

_SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


# ── the one attachments row ───────────────────────────────────────────────

def _is_count(v) -> bool:
    """An int >= 0 (a bool is not one)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


#: An attachments row's fields and what each must be: the documented U9 row
#: (every row carries these five), then the optional ones -- the store
#: purpose it was sent for and the offer's meta (INBOUND_META_KEYS).
_ROW_FIELDS: Tuple[Tuple[str, Callable[[Any], bool], str], ...] = (
    ('file_id', is_xid, 'a store xid (64 lowercase hex)'),
    ('sha256', is_xid, '64 lowercase hex'),
    ('name', is_file_name, 'a bare file name'),
    ('mime', lambda v: isinstance(v, str), 'a MIME type'),
    ('size', _is_count, 'an int >= 0'),
    ('purpose', lambda v: v in INBOUND_PURPOSES, 'one of %s' % (INBOUND_PURPOSES,)),
    ('captured_at', is_capture_ms, 'epoch milliseconds (an int > 0)'),
    ('duration_ms', _is_count, 'an int >= 0'),
)
_OPTIONAL_FIELDS = ('purpose',) + INBOUND_META_KEYS


def validate_attachment(item) -> List[str]:
    """Problems with one ``attachments`` row; empty when it is the ONE shape.

    The documented ConversationEntry.attachments row (U9; amendment A9 a)
    ``{file_id, sha256, name, mime, size}``: ``file_id`` is the store xid,
    which is the file's sha256, so ``sha256`` equals it; ``name`` is a bare
    file name; ``size`` an int >= 0.  Optional: ``purpose`` (an inbound
    purpose of the store, whose MIME list then holds ``mime``),
    ``captured_at`` (epoch ms, > 0) and ``duration_ms`` (an int >= 0).  Other
    keys are not read.  A reply's mirror row (no purpose) is valid as it
    stands.

    The one rule for a row (see the module docstring).  Pure and never
    raises; a problem names the field and what it must be, never the value,
    which is whatever a client sent.
    """
    if not isinstance(item, Mapping):
        return ['attachment is not an object']
    problems = []
    for key, ok, want in _ROW_FIELDS:
        if key not in item:
            if key not in _OPTIONAL_FIELDS:
                problems.append('attachment.%s is missing' % key)
        elif not ok(item[key]):
            problems.append('attachment.%s (a %s) is not %s'
                            % (key, type(item[key]).__name__, want))
    if problems:
        return problems
    if item['sha256'] != item['file_id']:
        return ['attachment.file_id must be the sha256 of the file']
    if 'purpose' in item and item['mime'] not in INBOUND_MIMES[item['purpose']]:
        return ['attachment.mime is not a type purpose %r takes (%s)'
                % (item['purpose'], ', '.join(sorted(INBOUND_MIMES[item['purpose']])))]
    return []


# ── inbound: the turn's references ────────────────────────────────────────

def parse_inbound(raw: Any) -> List[Dict[str, str]]:
    """The well-formed store references among a turn's ``attachments``.

    Reads at most MAX_ATTACHMENTS rows of a list and keeps each row that
    validate_attachment accepts, each file once.  Returns ``[{file_id}]`` --
    plus ``purpose`` when the row gave one -- in the order sent; the store's
    own record of the file is what a bound row then counts.  Anything else --
    not a list, a row the rule refuses -- is dropped, never raised on.  Pure:
    the store is not consulted (bind_inbound does that).
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning('chat_attachments: attachments is a %s, not a list; '
                           'none read', type(raw).__name__)
        return []
    if len(raw) > MAX_ATTACHMENTS:
        logger.warning('chat_attachments: %d attachments sent, the first %d read',
                       len(raw), MAX_ATTACHMENTS)
    kept: List[Dict[str, str]] = []
    seen = set()
    for item in raw[:MAX_ATTACHMENTS]:
        if validate_attachment(item):
            logger.info('chat_attachments: dropped an attachment row outside the '
                        'attachments shape')
            continue
        if item['file_id'] in seen:
            continue
        seen.add(item['file_id'])
        ref = {'file_id': item['file_id']}
        if 'purpose' in item:
            ref['purpose'] = item['purpose']
        kept.append(ref)
    return kept


def bind_inbound(user_id, prompt_id, request_id, items) -> List[Dict[str, Any]]:
    """Tie the parsed references of a turn to (user, prompt, request).

    Each reference binds the purpose it names, or, naming none, her chat
    attachment when the file is one, else the file's claim, unless that
    claim is bound to another turn (UserFileStore.bind); the file's other
    purposes stay on their own turns.  Only files in this user's own inbox,
    held for that purpose, are bound; another user's xid, an unknown one, a
    purpose the file was not sent for or a bare reference to a claim bound
    to another turn is dropped and logged.  Returns the bound files as the
    store's StoredFile dicts (``StoredFile.to_dict()``), in the order sent.
    Never raises: a store that refuses or fails binds nothing (of that
    purpose).
    """
    refs = [(it['file_id'], it.get('purpose')) for it in (items or ())
            if isinstance(it, Mapping) and is_xid(it.get('file_id'))]
    if not refs:
        return []
    bound = []
    for purpose in dict.fromkeys(p for _x, p in refs):      # each purpose named, in order
        group = [x for x, p in refs if p == purpose]
        try:
            bound += get_user_file_store().bind(user_id, group, prompt_id=prompt_id,
                                                request_id=request_id, purpose=purpose)
        except Exception as e:  # a bad id, a corrupt index, a full disk
            logger.warning('chat_attachments: could not bind %d attachment(s) for '
                           'user %r: %s', len(group), user_id, e)
    order: Dict[str, int] = {}
    for i, (x, _p) in enumerate(refs):
        order.setdefault(x, i)
    bound.sort(key=lambda sf: order[sf.xid])                # the order sent
    found = {sf.xid for sf in bound}
    dropped = [x[:12] for x, _p in refs if x not in found]
    if dropped:
        logger.warning('chat_attachments: dropped %d attachment(s) not bound for user %r '
                       '(not in the inbox, not held for the purpose named, or bound to '
                       'another turn and named with no purpose): %s',
                       len(dropped), user_id, ', '.join(dropped))
    return [sf.to_dict() for sf in bound]


def latest_inbound(user_id, prompt_id, *, request_id=None, purpose=REEL_SOURCE,
                   max_age_s: Optional[float] = 21600) -> List[Dict[str, Any]]:
    """The inbound files a reel turn may use, in upload order.

    Unconsumed files of ``purpose`` bound to this user's ``prompt_id``,
    uploaded within ``max_age_s``.  With ``request_id``: the ones bound to
    that turn.  Without: the newest batch -- the files bound to the same turn
    as the newest of them.  Returns StoredFile dicts
    (contracts.ClipSource.from_stored reads them).  Never raises.
    """
    try:
        files = get_user_file_store().query_inbox(
            user_id, prompt_id=prompt_id, request_id=request_id, purpose=purpose,
            max_age_s=max_age_s, unconsumed_only=True)
    except Exception as e:
        logger.warning('chat_attachments: could not read user %r\'s inbox: %s',
                       user_id, e)
        return []
    if request_id is None and files:
        newest = files[-1].request_id
        files = [f for f in files if f.request_id == newest]
    return [f.to_dict() for f in files]


# ── outbound: the turn's reel ─────────────────────────────────────────────

def take_outbound(user_id, prompt_id, request_id=None) -> List[Dict[str, Any]]:
    """The reply ``media`` items of this turn's outbound files, taken once.

    The store marks each file replied, so one reply carries it; a later
    reply, the push and the phone's file_list recovery are unaffected
    (they read ``fetched``, not ``replied``).  ``request_id=None`` takes any
    turn of the prompt.  A file whose record holds no media item is not in
    the reply (logged).  Never raises; no prompt is no media.
    """
    if prompt_id is None:
        return []
    try:
        taken = get_user_file_store().take_replied(user_id, prompt_id=prompt_id,
                                                   request_id=request_id)
    except Exception as e:
        logger.warning('chat_attachments: could not take user %r\'s outbound '
                       'media: %s', user_id, e)
        return []
    media = []
    for sf in taken:
        item = sf.meta.get('media')
        if isinstance(item, dict):
            media.append(item)
        else:
            logger.warning('chat_attachments: outbound %s has no media item; '
                           'not in the reply', sf.xid[:12])
    return media


def media_to_mirror(media) -> List[Dict[str, Any]]:
    """The chat mirror's attachment rows for reply ``media`` (pure).

    The documented ConversationEntry.attachments shape
    ``[{file_id, sha256, name, mime, size}]`` with ``file_id`` = the xid, so a
    device that dedups by xid sees the reply, the push and the mirror row as
    one reel -- the same shape a turn's ``attachments`` use: every row it
    writes is one validate_attachment accepts, so a later turn may send it
    back.  ``name`` is ``reel_<reel_id>.mp4`` (the xid's first 12 hex when the
    reel id is not a safe name).  An item without an xid, or whose row the
    rule refuses (no MIME type or size), is skipped.
    """
    rows = []
    for item in media if isinstance(media, (list, tuple)) else ():
        if not (isinstance(item, Mapping) and is_xid(item.get('xid'))):
            continue
        xid = item['xid']
        reel_id = item.get('reel_id')
        label = reel_id if isinstance(reel_id, str) and _SAFE_ID.fullmatch(reel_id) \
            else xid[:12]
        row = {'file_id': xid, 'sha256': xid, 'name': 'reel_%s.mp4' % label,
               'mime': item.get('mime'), 'size': item.get('size')}
        if not validate_attachment(row):
            rows.append(row)
    return rows
