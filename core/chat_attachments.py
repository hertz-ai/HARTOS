"""core.chat_attachments -- a chat turn's file references in, its reel out.

Reel Studio (plan WS-8a).  The phone uploads clips, a logo and song audio
into the user file store (core.user_file_store) over its device link, and a
/chat turn carries only references to them (plan 4.2(e)), in the ONE
attachments shape the chat mirror documents (ConversationEntry.attachments,
U9; amendment A9 a)::

    "attachments": [{"file_id": "<64-hex xid>", "sha256": "<the same>",
                     "name": ..., "mime": ..., "size": ...,
                     "purpose": "reel_source|logo|song_audio",   # optional
                     "captured_at": ..., "duration_ms": ...}]    # optional

``file_id`` is the store xid, which is the file's sha256.  The reel the turn
makes goes into the same store's outbox, and the reply that turn ends with
carries it as ``media`` (plan 4.2(f)) and its mirror row in the same shape
(media_to_mirror): what goes out is what comes in.

This module owns NO state.  It parses a turn's ``attachments`` (pure) and
delegates every read and write to the store, which is the one place files,
turn bindings, consumption and delivery are recorded:

    parse_inbound(raw)            the turn's well-formed references (pure)
    bind_inbound(...)             tie them to (user, prompt, request)
    latest_inbound(...)           what a reel turn may use
    take_outbound(...)            this turn's reel media, taken once
    media_to_mirror(media)        the chat mirror's attachment rows (pure)

The store-facing calls never raise: a /chat turn must not fail because a
reference was bad or the disk was unhappy.  A reference that does not resolve
to this user's inbox is dropped and logged.

LAYERING: core/ may not import integrations/, so the contract facts this
module needs (the inbound purposes) come from the store, and
tests/unit/test_chat_attachments.py holds them equal to
integrations.reel_studio.contracts.

parse_inbound is the looser of the two readers of a row.  It reads only
``file_id``, ``sha256`` and ``purpose``, so it keeps a row that
contracts.validate_attachment refuses for its other fields: one without
``name``, ``mime`` or ``size``, or with a name such as '../evil.mp4'.  That
is harmless here, since those fields are never read (the store's own record
of the file is what counts), but a row a client sends should still pass
validate_attachment.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

# _is_xid is the store's own rule for an xid (64 lowercase hex); asked here
# rather than copied, so a reference is well-formed exactly when the store
# would accept it.
from core.user_file_store import INBOUND_PURPOSES, _is_xid, get_user_file_store

logger = logging.getLogger(__name__)

#: At most this many attachments are read from one turn (plan 4.2(e); Nunba's
#: turn_attachments sends no more).
MAX_ATTACHMENTS = 10

_SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


# ── inbound: the turn's references ────────────────────────────────────────

def _is_store_ref(item) -> bool:
    """A row naming a file in the store: ``file_id`` is an xid and ``sha256``
    is the same xid; ``purpose``, when given, is an inbound purpose."""
    return (isinstance(item, Mapping) and _is_xid(item.get('file_id'))
            and item.get('sha256') == item['file_id']
            and ('purpose' not in item or item['purpose'] in INBOUND_PURPOSES))


def parse_inbound(raw: Any) -> List[Dict[str, str]]:
    """The well-formed store references among a turn's ``attachments``.

    Reads at most MAX_ATTACHMENTS rows of a list and keeps each row that
    names a store file (_is_store_ref), each file once.  Returns
    ``[{file_id}]`` -- plus ``purpose`` when the row gave one -- in the order
    sent.  The row's name, mime and size are not read: the store's own record
    of the file is what counts.  Anything else -- not a list, a malformed
    row -- is dropped, never raised on.  Pure: the store is not consulted
    (bind_inbound does that).
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
        if not _is_store_ref(item):
            logger.info('chat_attachments: dropped a malformed attachment entry')
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

    Only files in this user's own inbox are bound; another user's xid or an
    unknown one is dropped and logged.  Returns the bound files as the
    store's StoredFile dicts (``StoredFile.to_dict()``), in the order sent.
    Never raises: a store that refuses or fails binds nothing.
    """
    refs = [it['file_id'] for it in (items or ())
            if isinstance(it, Mapping) and _is_xid(it.get('file_id'))]
    if not refs:
        return []
    try:
        bound = get_user_file_store().bind(user_id, refs, prompt_id=prompt_id,
                                           request_id=request_id)
    except Exception as e:  # a bad id, a corrupt index, a full disk
        logger.warning('chat_attachments: could not bind %d attachment(s) for '
                       'user %r: %s', len(refs), user_id, e)
        return []
    found = {sf.xid for sf in bound}
    dropped = [r[:12] for r in refs if r not in found]
    if dropped:
        logger.warning('chat_attachments: dropped %d attachment(s) not in user '
                       '%r\'s inbox: %s', len(dropped), user_id, ', '.join(dropped))
    return [sf.to_dict() for sf in bound]


def latest_inbound(user_id, prompt_id, *, request_id=None, purpose='reel_source',
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
    one reel -- the same shape a turn's ``attachments`` use (parse_inbound).
    ``name`` is ``reel_<reel_id>.mp4`` (the xid's first 12 hex when the reel
    id is not a safe name).  Items without an xid are skipped.
    """
    rows = []
    for item in media if isinstance(media, (list, tuple)) else ():
        if not (isinstance(item, Mapping) and _is_xid(item.get('xid'))):
            continue
        xid = item['xid']
        reel_id = item.get('reel_id')
        label = reel_id if isinstance(reel_id, str) and _SAFE_ID.fullmatch(reel_id) \
            else xid[:12]
        rows.append({'file_id': xid, 'sha256': xid, 'name': 'reel_%s.mp4' % label,
                     'mime': item.get('mime'), 'size': item.get('size')})
    return rows
