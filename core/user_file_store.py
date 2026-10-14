"""core.user_file_store -- the ONE store for files between a user's phone and this desktop.

Inbound, the phone uploads source clips, a logo and song audio in chunks
(``offer`` / ``put_chunk`` / ``commit`` / ``abort``).  Outbound, a finished
reel goes into an outbox (``offer_to_device``) that the phone pulls in chunks
(``get_chunk``), lists for recovery (``list_outbox``) and acknowledges
(``mark_fetched``).  In between, the store binds inbound files to a chat turn
(``bind`` / ``query_inbox`` / ``mark_consumed``), hands each outbound reel to
one chat reply (``take_replied``), enforces the per-user quota and runs the
only GC (``gc``).  Everything else (the PeerLink door, core.chat_attachments,
the reel pipeline) delegates here and keeps no file state of its own.

Layout, under ``get_data_dir()/user_files/`` (never the uploads dir, which
the desktop serves without auth)::

    <sha256(user_id)[:16]>/index.json     committed files, in and out
                           in/<xid><ext>   inbound files
                           out/<xid><ext>  outbound files
                           part/<xid>.part + <xid>.json   uploads in progress

``xid`` is the sha256 of the whole file (64 lowercase hex), scoped to the
user.  Files are named by xid, never by the name the phone sent, so a name
can never steer a write.  ``index.json`` and each upload's sidecar are
written with core.file_cache.atomic_json_write; this module is their only
writer.  The index is read from disk on every call (no in-process cache), so
a restart, or a second UserFileStore on the same root, sees everything.

Uploads resume.  The sidecar keeps a bitmap of the chunks written, so the
same offer made again (after a dropped link, a relay/LAN switch or a phone
restart) answers with only the missing chunk ranges.  A resume at another
chunk size keeps every byte already written.

Door-facing calls (offer, put_chunk, commit, abort, get_chunk) answer with a
frame-shaped dict and never raise on what the phone sent: a bad offer or
chunk is refused with a ``reason`` (REFUSALS).  A bad ``user_id`` or a
chunk size outside the policy is the caller's bug and raises ValueError.

Concurrency: one lock per user directory, shared by every UserFileStore in
the process; commit hashes the file outside it.  One process writes a
user's directory (the desktop app); a second process (a CLI) must not write
while the app is running.

Transport-agnostic: no PeerLink import, and core/ never imports
integrations/.  So the inbound facts the contracts module also states
(INBOUND_PURPOSES, INBOUND_MIMES, INBOUND_META_KEYS, OUTBOUND_PURPOSE) are
repeated here, and tests/unit/test_user_file_store.py asserts they equal
integrations.reel_studio.contracts.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from core.file_cache import atomic_json_write
from core.platform_paths import get_data_dir

logger = logging.getLogger(__name__)

SCHEMA = 'hart.user_files.v1'
PART_SCHEMA = 'hart.user_files.part.v1'

# ── what may be uploaded ──────────────────────────────────────────────────

#: Accepted MIME types per inbound purpose, with the extension the stored
#: file gets.  audio/x-wav is what Android's MediaStore reports for WAV.
_INBOUND_TYPES = {
    'reel_source': {'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/3gpp': '.3gp'},
    'logo': {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'},
    'song_audio': {'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
                   'audio/ogg': '.ogg', 'audio/wav': '.wav', 'audio/x-wav': '.wav'},
}
INBOUND_PURPOSES = tuple(_INBOUND_TYPES)
INBOUND_MIMES = MappingProxyType({p: frozenset(m) for p, m in _INBOUND_TYPES.items()})
#: Optional offer fields kept in StoredFile.meta: ``captured_at`` (the phone's
#: MediaStore DATE_TAKEN, epoch ms; amendment A1) and ``duration_ms``.
INBOUND_META_KEYS = ('captured_at', 'duration_ms')
OUTBOUND_PURPOSE = 'reel'
_EXT = MappingProxyType({mime: ext for types in _INBOUND_TYPES.values()
                         for mime, ext in types.items()})

BOXES = ('in', 'out')

#: Every ``reason`` a ``file_refused`` answer can carry.
REFUSALS = ('bad_offer', 'bad_name', 'bad_mime', 'too_large', 'quota', 'no_space', 'busy',
            'chunk_corrupt', 'unknown_xid', 'out_of_range', 'not_found')

#: Limits and retention windows (plan 4.1 and U4/finding 23).  Logos are
#: kept until a newer logo replaces them.
POLICY = MappingProxyType({
    'max_file': 1 << 30,              # one upload
    'max_active_uploads': 2,          # uploads in progress per user
    'upload_idle_s': 600,             # an upload untouched this long stops counting as active
    'user_quota': 5 << 30,            # held files + open uploads, per user
    'free_margin': 2 << 30,           # disk left free after an upload
    'chunk_size': 1 << 20,            # default; the door passes 256 KiB on relayed links
    'min_chunk_size': 64 << 10,
    'max_chunk_size': 4 << 20,
    'max_inflight': 3,                # puts in flight, told to the phone
    'part_ttl_s': 48 * 3600,          # an upload untouched this long is removed
    'source_after_use_s': 24 * 3600,  # a consumed clip or song is removed this long after use
    'source_unused_s': 72 * 3600,     # ... or this long after upload if never used
    'outbox_after_fetch_s': 48 * 3600,
    'outbox_max_s': 7 * 24 * 3600,
})

_HASH_BLOCK = 1 << 20
_HEX64 = re.compile(r'[0-9a-f]{64}')


@dataclass
class StoredFile:
    """One committed file, as the index records it.

    ``box`` is 'in' (from the phone) or 'out' (to the phone); ``sha256`` is
    ``xid``.  ``prompt_id`` and ``request_id`` bind it to a chat turn (both
    strings).  ``meta`` holds the offer's captured_at/duration_ms for an
    inbound file, and the caller's meta (the reply ``media`` item) for an
    outbound one.
    """
    xid: str
    user_id: str
    box: str
    path: str
    name: str
    mime: str
    size: int
    sha256: str
    purpose: str
    created_at: float
    prompt_id: Optional[str]
    request_id: Optional[str]
    meta: dict
    consumed_at: Optional[float]
    replied: bool
    fetched: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── small predicates and helpers ──────────────────────────────────────────

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_xid(v) -> bool:
    return isinstance(v, str) and _HEX64.fullmatch(v) is not None


def _is_file_name(v) -> bool:
    """A bare file name: no directories, no traversal, no control characters.

    The same rule as integrations.reel_studio.contracts (a test holds them
    equal).  The store never builds a path from it."""
    return (isinstance(v, str) and 0 < len(v) <= 255 and v not in ('.', '..')
            and not any(c in v for c in '/\\') and all(ord(c) >= 32 for c in v))


def _user_key(user_id) -> str:
    """The user id as a string (10077 and '10077' are one user)."""
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        raise ValueError('user_id must be a string or an int, got %r' % (user_id,))
    text = str(user_id).strip()
    if not text:
        raise ValueError('user_id is empty')
    return text


def _turn_id(value, what: str) -> Optional[str]:
    """A prompt or request id as a string (123 and '123' are one id); None when absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('%s must be a string or an int, got %r' % (what, value))
    return str(value).strip() or None


def _inbound_meta(meta) -> Dict[str, int]:
    """The INBOUND_META_KEYS of an offer that hold usable values.

    A bad captured_at or duration_ms is dropped, not refused: it is an
    ordering hint, and the clip is still worth having without it."""
    if not isinstance(meta, Mapping):
        return {}
    out = {}
    captured, duration = meta.get('captured_at'), meta.get('duration_ms')
    if _is_int(captured) and captured > 0:
        out['captured_at'] = captured
    elif captured is not None:
        logger.info('user_file_store: dropped captured_at=%r (not epoch ms)', captured)
    if _is_int(duration) and duration >= 0:
        out['duration_ms'] = duration
    elif duration is not None:
        logger.info('user_file_store: dropped duration_ms=%r', duration)
    return out


def _refused(reason: str, xid=None, i=None) -> Dict[str, Any]:
    out = {'type': 'file_refused', 'reason': reason}
    if _is_xid(xid):
        out['xid'] = xid
    if i is not None:
        out['i'] = i
    return out


def _n_chunks(size: int, chunk_size: int) -> int:
    return (size + chunk_size - 1) // chunk_size


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(_HASH_BLOCK), b''):
            digest.update(block)
    return digest.hexdigest()


def _write_at(path: str, offset: int, data: bytes) -> None:
    """Write ``data`` at ``offset`` of an existing file and make it durable.

    Seek-and-write (os.pwrite does not exist on Windows); callers hold the
    user's lock.  Durable before the sidecar marks the chunk done."""
    with open(path, 'r+b') as f:
        f.seek(offset)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _remove(path: str) -> bool:
    """Delete a file; True when it is gone.  A file another handle holds
    open (Windows) stays, and the next GC tries again."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.warning('user_file_store: could not remove %s: %s', path, e)
        return False


# ── chunk bitmaps ─────────────────────────────────────────────────────────

def _bits_new(n: int) -> bytearray:
    return bytearray((n + 7) // 8)


def _has(bits: bytearray, i: int) -> bool:
    return bool(bits[i >> 3] >> (i & 7) & 1)


def _mark(bits: bytearray, i: int) -> None:
    bits[i >> 3] |= 1 << (i & 7)


def _encode_bits(bits: bytearray) -> str:
    return base64.b64encode(bytes(bits)).decode('ascii')


def _decode_bits(text, n: int) -> Optional[bytearray]:
    try:
        bits = bytearray(base64.b64decode(text, validate=True))
    except (TypeError, ValueError):
        return None
    return bits if len(bits) == (n + 7) // 8 else None


def _missing(bits: bytearray, n: int) -> List[List[int]]:
    """The chunks not yet written, as inclusive [first, last] ranges."""
    out: List[List[int]] = []
    for i in range(n):
        if _has(bits, i):
            continue
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def _done_bytes(bits: bytearray, n: int, chunk_size: int, size: int) -> int:
    return sum(min(chunk_size, size - i * chunk_size) for i in range(n) if _has(bits, i))


def _remap(bits: bytearray, n: int, old_cs: int, new_cs: int, size: int) -> bytearray:
    """The same written bytes as a bitmap of ``new_cs`` chunks: a new chunk
    counts as written when every byte of it was written."""
    spans: List[List[int]] = []
    for i in range(n):
        if _has(bits, i):
            start, end = i * old_cs, min((i + 1) * old_cs, size)
            if spans and spans[-1][1] == start:
                spans[-1][1] = end
            else:
                spans.append([start, end])
    new_n = _n_chunks(size, new_cs)
    out = _bits_new(new_n)
    k = 0
    for j in range(new_n):
        start, end = j * new_cs, min((j + 1) * new_cs, size)
        while k < len(spans) and spans[k][1] <= start:
            k += 1
        if k < len(spans) and spans[k][0] <= start and end <= spans[k][1]:
            _mark(out, j)
    return out


# ── per-user locks, shared by every store in the process ──────────────────

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
#: (user dir, xid) pairs whose commit is hashing the file outside the lock.
_COMMITTING: set = set()


def _dir_key(user_dir: str) -> str:
    return os.path.normcase(os.path.abspath(user_dir))


def _lock_for(user_dir: str) -> threading.RLock:
    key = _dir_key(user_dir)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


# ── the index ─────────────────────────────────────────────────────────────

_RECORD_TYPES = {
    'file': str, 'name': str, 'mime': str, 'size': int, 'purpose': str, 'seq': int,
    'created_at': (int, float), 'meta': dict, 'replied': bool, 'fetched': bool,
}


def _record_ok(rec) -> bool:
    return (isinstance(rec, dict)
            and all(isinstance(rec.get(k), t) and not (t is int and isinstance(rec.get(k), bool))
                    for k, t in _RECORD_TYPES.items())
            and _is_file_name(rec['file'])
            and all(rec.get(k) is None or _is_num(rec.get(k))
                    for k in ('consumed_at', 'fetched_at'))
            and all(rec.get(k) is None or isinstance(rec.get(k), str)
                    for k in ('prompt_id', 'request_id')))


def _empty_index(uid: Optional[str]) -> Dict[str, Any]:
    return {'schema': SCHEMA, 'user_id': uid, 'seq': 0, 'files': {'in': {}, 'out': {}}}


class UserFileStore:
    """The user file store.  See the module docstring.

    ``root`` defaults to ``get_data_dir()/user_files``, resolved on each
    call.  ``policy`` overrides POLICY keys (tests, operators).
    ``disk_free(path) -> bytes`` and ``clock() -> epoch seconds`` are
    injectable for tests.
    """

    def __init__(self, root: Optional[str] = None, *, policy: Optional[Mapping] = None,
                 disk_free: Optional[Callable[[str], int]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        unknown = sorted(set(policy or ()) - set(POLICY))
        if unknown:
            raise ValueError('unknown policy keys %s (known: %s)' % (unknown, sorted(POLICY)))
        self._root = root
        self.policy = MappingProxyType({**POLICY, **(policy or {})})
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self._clock = clock or time.time

    @property
    def root(self) -> str:
        return self._root or os.path.join(get_data_dir(), 'user_files')

    # ── paths ──

    def _user_dir(self, uid: str) -> str:
        return os.path.join(self.root, hashlib.sha256(uid.encode('utf-8')).hexdigest()[:16])

    @staticmethod
    def _part_paths(user_dir: str, xid: str) -> Tuple[str, str]:
        base = os.path.join(user_dir, 'part', xid)
        return base + '.part', base + '.json'

    @staticmethod
    def _file_path(user_dir: str, box: str, rec: Mapping) -> str:
        return os.path.join(user_dir, box, rec['file'])

    def _free_bytes(self) -> int:
        """Free disk where the store lives (the root is created first:
        shutil.disk_usage needs a path that exists)."""
        root = self.root
        os.makedirs(root, exist_ok=True)
        return self._disk_free(root)

    def _chunk_size(self, value) -> int:
        if value is None:
            return self.policy['chunk_size']
        lo, hi = self.policy['min_chunk_size'], self.policy['max_chunk_size']
        if not _is_int(value) or not lo <= value <= hi:
            raise ValueError('chunk_size must be an int in %d..%d, got %r' % (lo, hi, value))
        return value

    # ── index I/O (caller holds the user's lock) ──

    def _load_index(self, user_dir: str, uid: Optional[str]) -> Dict[str, Any]:
        path = os.path.join(user_dir, 'index.json')
        try:
            with open(path, encoding='utf-8') as f:
                doc = json.load(f)
        except FileNotFoundError:
            return _empty_index(uid)
        except ValueError as e:
            return self._set_aside(user_dir, uid, 'not JSON (%s)' % e)
        if not (isinstance(doc, dict) and doc.get('schema') == SCHEMA
                and _is_int(doc.get('seq')) and isinstance(doc.get('files'), dict)
                and all(isinstance(doc['files'].get(b), dict) for b in BOXES)):
            return self._set_aside(user_dir, uid, 'not a %s index' % SCHEMA)
        if uid is not None and doc.get('user_id') != uid:
            # sha256(user_id)[:16] collided: never show one user another's files.
            raise PermissionError('user_file_store: %s belongs to another user' % user_dir)
        for box in BOXES:
            records = doc['files'][box]
            for xid in [x for x, rec in records.items() if not (_is_xid(x) and _record_ok(rec))]:
                logger.error('user_file_store: dropped a malformed %s record %r in %s',
                             box, xid, path)
                del records[xid]
        return doc

    def _set_aside(self, user_dir: str, uid: Optional[str], why: str) -> Dict[str, Any]:
        """Move an unreadable index out of the way and start an empty one.

        Its files become unreferenced and the GC removes them once they are
        older than part_ttl_s; the phone uploads again."""
        path = os.path.join(user_dir, 'index.json')
        aside = os.path.join(user_dir, 'index.corrupt.%s.json' % uuid.uuid4().hex[:8])
        try:
            os.replace(path, aside)
        except OSError as e:
            logger.error('user_file_store: index %s is unreadable (%s) and could not be '
                         'moved aside: %s', path, why, e)
            raise
        logger.error('user_file_store: index %s is unreadable (%s); moved to %s and '
                     'starting empty', path, why, aside)
        return _empty_index(uid)

    @staticmethod
    def _save_index(user_dir: str, doc: Mapping) -> None:
        atomic_json_write(os.path.join(user_dir, 'index.json'), doc)

    def _stored(self, user_dir: str, uid: str, box: str, xid: str, rec: Mapping) -> StoredFile:
        return StoredFile(
            xid=xid, user_id=uid, box=box, path=self._file_path(user_dir, box, rec),
            name=rec['name'], mime=rec['mime'], size=rec['size'], sha256=xid,
            purpose=rec['purpose'], created_at=rec['created_at'],
            prompt_id=rec.get('prompt_id'), request_id=rec.get('request_id'),
            meta=json.loads(json.dumps(rec['meta'])), consumed_at=rec.get('consumed_at'),
            replied=rec['replied'], fetched=rec['fetched'])

    def _live(self, user_dir: str, doc: Mapping, box: str) -> List[Tuple[str, dict]]:
        """(xid, record) of a box whose file exists, in commit order."""
        rows = [(x, r) for x, r in doc['files'][box].items()
                if os.path.isfile(self._file_path(user_dir, box, r))]
        return sorted(rows, key=lambda row: row[1]['seq'])

    # ── upload sidecars (caller holds the user's lock) ──

    def _load_part(self, user_dir: str, xid: str) -> Optional[Dict[str, Any]]:
        data_path, side_path = self._part_paths(user_dir, xid)
        try:
            with open(side_path, encoding='utf-8') as f:
                part = json.load(f)
        except FileNotFoundError:
            return None
        except ValueError:
            part = None
        ok = (isinstance(part, dict) and part.get('schema') == PART_SCHEMA
              and part.get('xid') == xid and _is_int(part.get('size')) and part['size'] > 0
              and _is_int(part.get('chunk_size')) and part['chunk_size'] > 0
              and part.get('n') == _n_chunks(part['size'], part['chunk_size'])
              and _is_num(part.get('touched_at')) and isinstance(part.get('meta'), dict))
        bits = _decode_bits(part.get('have'), part['n']) if ok else None
        if bits is None:
            logger.error('user_file_store: unreadable upload sidecar %s; upload removed',
                         side_path)
            _remove(data_path)
            _remove(side_path)
            return None
        part['bits'] = bits
        if not os.path.isfile(data_path):
            # The bytes are gone: the upload starts over, and the sidecar says
            # so, or a later read would trust its old bitmap.
            logger.warning('user_file_store: upload %s lost its bytes; starting it over',
                           xid[:12])
            with open(data_path, 'wb'):
                pass
            part['bits'] = _bits_new(part['n'])
            try:
                self._save_part(user_dir, part)
            except OSError as e:
                logger.warning('user_file_store: could not reset sidecar %s: %s', side_path, e)
        return part

    def _save_part(self, user_dir: str, part: Dict[str, Any]) -> None:
        doc = {k: v for k, v in part.items() if k != 'bits'}
        doc['have'] = _encode_bits(part['bits'])
        atomic_json_write(self._part_paths(user_dir, part['xid'])[1], doc)

    def _parts(self, user_dir: str) -> List[Dict[str, Any]]:
        part_dir = os.path.join(user_dir, 'part')
        try:
            names = os.listdir(part_dir)
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext == '.json' and _is_xid(stem):
                part = self._load_part(user_dir, stem)
                if part is not None:
                    out.append(part)
        return out

    def _usage(self, user_dir: str, doc: Mapping) -> int:
        held = sum(r['size'] for box in BOXES for r in doc['files'][box].values())
        return held + sum(p['size'] for p in self._parts(user_dir))

    # ── inbound: the phone uploads ──

    def offer(self, user_id, name, size, sha256, mime, purpose, *,
              meta: Optional[Mapping] = None, chunk_size: Optional[int] = None) -> Dict[str, Any]:
        """Start or resume an upload.

        Answers ``file_accept {xid, chunk_size, n, missing, max_inflight}``
        (``missing``: inclusive [first, last] chunk ranges still needed),
        ``file_have {xid}`` when the bytes are already held (the record is
        refreshed as if uploaded again: name, purpose, meta, and no longer
        consumed), or ``file_refused {reason}``.  ``meta`` may be the whole
        offer frame: only INBOUND_META_KEYS with usable values are kept.
        ``chunk_size`` is the door's (smaller on relayed links); a resume at
        another size keeps the bytes already written.  Runs this user's GC
        first, so stale uploads and expired files do not hold the quota.
        """
        uid = _user_key(user_id)
        xid = sha256 if _is_xid(sha256) else None
        if xid is None or not _is_int(size) or size <= 0 or purpose not in INBOUND_PURPOSES:
            return _refused('bad_offer', xid)
        if not _is_file_name(name):
            return _refused('bad_name', xid)
        if not isinstance(mime, str) or mime not in INBOUND_MIMES[purpose]:
            return _refused('bad_mime', xid)
        if size > self.policy['max_file']:
            return _refused('too_large', xid)
        cs = self._chunk_size(chunk_size)
        keep = _inbound_meta(meta)
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            now = self._clock()
            doc = self._load_index(user_dir, uid)
            self._gc_user(user_dir, doc, now)
            held = doc['files']['in'].get(xid)
            if held is not None and os.path.isfile(self._file_path(user_dir, 'in', held)):
                doc['seq'] += 1
                held.update(name=name, mime=mime, purpose=purpose, consumed_at=None,
                            created_at=now, seq=doc['seq'], meta={**held['meta'], **keep})
                self._save_index(user_dir, doc)
                return {'type': 'file_have', 'xid': xid}
            if (_dir_key(user_dir), xid) in _COMMITTING:
                return _refused('busy', xid)
            part = self._load_part(user_dir, xid)
            if part is not None and part['size'] != size:
                self._drop_part(user_dir, xid)
                part = None
            fresh = part is None
            if fresh:
                refusal = self._admit(user_dir, doc, size, now)
                if refusal:
                    return _refused(refusal, xid)
            else:
                if part['chunk_size'] != cs:
                    part['bits'] = _remap(part['bits'], part['n'], part['chunk_size'], cs, size)
                    part['chunk_size'], part['n'] = cs, _n_chunks(size, cs)
                still = size - _done_bytes(part['bits'], part['n'], cs, size)
                if self._free_bytes() - still < self.policy['free_margin']:
                    return _refused('no_space', xid)
            try:
                if fresh:
                    part = self._new_part(user_dir, uid, xid, size, cs, now)
                part.update(name=name, mime=mime, purpose=purpose, touched_at=now,
                            meta={**part['meta'], **keep})
                self._save_part(user_dir, part)
            except OSError as e:
                if e.errno != errno.ENOSPC:
                    raise
                if fresh:
                    self._drop_part(user_dir, xid)
                return _refused('no_space', xid)
            return {'type': 'file_accept', 'xid': xid, 'chunk_size': part['chunk_size'],
                    'n': part['n'], 'missing': _missing(part['bits'], part['n']),
                    'max_inflight': self.policy['max_inflight']}

    def _admit(self, user_dir: str, doc: Mapping, size: int, now: float) -> Optional[str]:
        """Why a NEW upload of ``size`` bytes may not start, or None."""
        idle = self.policy['upload_idle_s']
        active = [p for p in self._parts(user_dir) if now - p['touched_at'] < idle]
        if len(active) >= self.policy['max_active_uploads']:
            return 'busy'
        if self._usage(user_dir, doc) + size > self.policy['user_quota']:
            return 'quota'
        if self._free_bytes() - size < self.policy['free_margin']:
            return 'no_space'
        return None

    def _new_part(self, user_dir: str, uid: str, xid: str, size: int, cs: int,
                  now: float) -> Dict[str, Any]:
        data_path, _ = self._part_paths(user_dir, xid)
        os.makedirs(os.path.dirname(data_path), exist_ok=True)
        with open(data_path, 'wb'):
            pass
        n = _n_chunks(size, cs)
        return {'schema': PART_SCHEMA, 'user_id': uid, 'xid': xid, 'size': size,
                'chunk_size': cs, 'n': n, 'bits': _bits_new(n), 'meta': {},
                'started_at': now, 'touched_at': now}

    def _drop_part(self, user_dir: str, xid: str) -> bool:
        data_path, side_path = self._part_paths(user_dir, xid)
        gone = _remove(data_path)
        return _remove(side_path) and gone

    def put_chunk(self, user_id, xid, i, data, chunk_sha256) -> Dict[str, Any]:
        """Write chunk ``i`` of an upload.  ``file_ack {xid, i}`` or
        ``file_refused {reason, xid, i}``.  A chunk already written is acked
        again and changes nothing; a chunk whose sha256 or length is wrong is
        refused as ``chunk_corrupt`` and stays missing."""
        uid = _user_key(user_id)
        if not _is_xid(xid):
            return _refused('unknown_xid', None, i)
        payload = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else None
        digest = hashlib.sha256(payload).hexdigest() if payload is not None else None
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            part = self._load_part(user_dir, xid)
            if part is None:
                if self._held(user_dir, uid, xid):
                    return {'type': 'file_ack', 'xid': xid, 'i': i}   # a late duplicate
                return _refused('unknown_xid', xid, i)
            n, cs, size = part['n'], part['chunk_size'], part['size']
            if not _is_int(i) or not 0 <= i < n:
                return _refused('out_of_range', xid, i)
            if (payload is None or digest != chunk_sha256
                    or len(payload) != min(cs, size - i * cs)):
                return _refused('chunk_corrupt', xid, i)
            try:
                if not _has(part['bits'], i):
                    _write_at(self._part_paths(user_dir, xid)[0], i * cs, payload)
                    _mark(part['bits'], i)
                part['touched_at'] = self._clock()
                self._save_part(user_dir, part)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    return _refused('no_space', xid, i)   # the chunk stays missing
                raise
            return {'type': 'file_ack', 'xid': xid, 'i': i}

    def _held(self, user_dir: str, uid: str, xid: str, box: str = 'in') -> Optional[dict]:
        rec = self._load_index(user_dir, uid)['files'][box].get(xid)
        return rec if rec is not None and os.path.isfile(self._file_path(user_dir, box, rec)) \
            else None

    def commit(self, user_id, xid) -> Dict[str, Any]:
        """Finish an upload: ``file_done {xid}`` once the whole file hashes to
        ``xid``; ``file_corrupt {xid, missing}`` when chunks are missing, or
        when every chunk is there but the file does not hash to ``xid`` (the
        upload then starts over: ``missing`` is every chunk).  The file is
        hashed outside the user's lock; meanwhile an abort of it is ``busy``.
        """
        uid = _user_key(user_id)
        if not _is_xid(xid):
            return _refused('unknown_xid')
        user_dir = self._user_dir(uid)
        key = (_dir_key(user_dir), xid)
        lock = _lock_for(user_dir)
        with lock:
            part = self._load_part(user_dir, xid)
            if part is None:
                if self._held(user_dir, uid, xid):
                    return {'type': 'file_done', 'xid': xid}
                return _refused('unknown_xid', xid)
            if key in _COMMITTING:
                return _refused('busy', xid)
            missing = _missing(part['bits'], part['n'])
            if missing:
                return {'type': 'file_corrupt', 'xid': xid, 'missing': missing}
            _COMMITTING.add(key)
        try:
            data_path = self._part_paths(user_dir, xid)[0]
            try:
                intact = (os.path.getsize(data_path) == part['size']
                          and _file_sha256(data_path) == xid)
            except OSError as e:
                logger.warning('user_file_store: could not hash upload %s: %s', data_path, e)
                intact = False
            with lock:
                part = self._load_part(user_dir, xid)
                if part is None:
                    return _refused('unknown_xid', xid)
                if not intact:
                    logger.warning('user_file_store: upload %s does not hash to its xid; '
                                   'starting it over', xid[:12])
                    with open(data_path, 'wb'):
                        pass
                    part['bits'] = _bits_new(part['n'])
                    part['touched_at'] = self._clock()
                    self._save_part(user_dir, part)
                    return {'type': 'file_corrupt', 'xid': xid, 'missing': [[0, part['n'] - 1]]}
                self._file_away(user_dir, uid, xid, part, data_path)
                return {'type': 'file_done', 'xid': xid}
        finally:
            with lock:
                _COMMITTING.discard(key)

    def _file_away(self, user_dir: str, uid: str, xid: str, part: Mapping,
                   data_path: str) -> None:
        """Move a verified upload into the inbox (caller holds the lock).

        The record is written first: a crash before the rename leaves a
        record without a file, which every reader skips and the next commit
        of the same upload rewrites."""
        doc = self._load_index(user_dir, uid)
        doc['seq'] += 1
        name = xid + _EXT.get(part['mime'], '')
        doc['files']['in'][xid] = {
            'file': name, 'name': part['name'], 'mime': part['mime'], 'size': part['size'],
            'purpose': part['purpose'], 'seq': doc['seq'], 'created_at': self._clock(),
            'meta': dict(part['meta']), 'prompt_id': None, 'request_id': None,
            'consumed_at': None, 'replied': False, 'fetched': False, 'fetched_at': None,
        }
        self._save_index(user_dir, doc)
        os.makedirs(os.path.join(user_dir, 'in'), exist_ok=True)
        os.replace(data_path, os.path.join(user_dir, 'in', name))
        _remove(self._part_paths(user_dir, xid)[1])

    def abort(self, user_id, xid) -> Dict[str, Any]:
        """Drop an upload in progress and its sidecar: ``file_aborted {xid}``
        (also when there was none).  Committed files are not touched."""
        uid = _user_key(user_id)
        if not _is_xid(xid):
            return _refused('unknown_xid')
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            if (_dir_key(user_dir), xid) in _COMMITTING:
                return _refused('busy', xid)
            self._drop_part(user_dir, xid)
        return {'type': 'file_aborted', 'xid': xid}

    # ── inbound: turns use the files ──

    def bind(self, user_id, xids: Iterable[str], *, prompt_id, request_id) -> List[StoredFile]:
        """Tie inbound files to a chat turn; returns the bound files in the
        order given.  An xid that is not in this user's inbox is skipped and
        logged.  Binding a consumed file makes it usable again (the turn
        asked for it)."""
        uid = _user_key(user_id)
        pid, rid = _turn_id(prompt_id, 'prompt_id'), _turn_id(request_id, 'request_id')
        if isinstance(xids, str):
            xids = [xids]
        wanted: List[str] = []
        for x in xids or ():
            if _is_xid(x) and x not in wanted:
                wanted.append(x)
            elif not _is_xid(x):
                logger.info('user_file_store: bind skipped a malformed ref %r', x)
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            bound = []
            for x in wanted:
                rec = doc['files']['in'].get(x)
                if rec is None or not os.path.isfile(self._file_path(user_dir, 'in', rec)):
                    logger.info('user_file_store: bind skipped %s, not in the inbox', x[:12])
                    continue
                rec.update(prompt_id=pid, request_id=rid, consumed_at=None)
                bound.append(self._stored(user_dir, uid, 'in', x, rec))
            if bound:
                self._save_index(user_dir, doc)
            return bound

    def query_inbox(self, user_id, *, prompt_id=None, request_id=None, purpose=None,
                    max_age_s=None, unconsumed_only=True) -> List[StoredFile]:
        """Inbound files in upload (commit) order.  Each filter applies only
        when given: ``prompt_id=None`` means any prompt, not unbound files."""
        uid = _user_key(user_id)
        pid, rid = _turn_id(prompt_id, 'prompt_id'), _turn_id(request_id, 'request_id')
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            now = self._clock()
            doc = self._load_index(user_dir, uid)
            return [self._stored(user_dir, uid, 'in', x, r)
                    for x, r in self._live(user_dir, doc, 'in')
                    if (pid is None or r.get('prompt_id') == pid)
                    and (rid is None or r.get('request_id') == rid)
                    and (purpose is None or r['purpose'] == purpose)
                    and (max_age_s is None or now - r['created_at'] <= max_age_s)
                    and (not unconsumed_only or r.get('consumed_at') is None)]

    def mark_consumed(self, user_id, xids: Iterable[str]) -> None:
        """Record that a reel used these inbound files now; the GC removes
        them source_after_use_s later (logos excepted)."""
        uid = _user_key(user_id)
        if isinstance(xids, str):
            xids = [xids]
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            now, changed = self._clock(), False
            for x in xids or ():
                rec = doc['files']['in'].get(x) if _is_xid(x) else None
                if rec is not None:
                    rec['consumed_at'] = now
                    changed = True
            if changed:
                self._save_index(user_dir, doc)

    # ── outbound: the desktop sends a file to the phone ──

    def offer_to_device(self, user_id, path, *, mime, purpose, prompt_id, request_id,
                        meta: Optional[Mapping] = None, name: Optional[str] = None) -> StoredFile:
        """Copy ``path`` into the outbox and record it for the phone.

        The caller's file is not changed (its work dir may be deleted
        afterwards).  ``xid`` is the copy's sha256.  When ``meta['media']``
        is a reply media item, the store writes its own facts into it (xid,
        sha256, size, mime, prompt_id, request_id), so the item and the file
        cannot disagree.  ``name`` defaults to the source's base name.  The
        same bytes offered again are one entry, rebound to the new turn and
        due for delivery again.  Raises ValueError for a purpose other than
        OUTBOUND_PURPOSE, a bad name or an empty file, and OSError when the
        source cannot be read or the disk is full.
        """
        uid = _user_key(user_id)
        if purpose != OUTBOUND_PURPOSE:
            raise ValueError('outbound purpose must be %r, got %r' % (OUTBOUND_PURPOSE, purpose))
        if not isinstance(mime, str) or not mime:
            raise ValueError('mime must be a MIME type, got %r' % (mime,))
        name = os.path.basename(path) if name is None else name
        if not _is_file_name(name):
            raise ValueError('name must be a bare file name, got %r' % (name,))
        pid, rid = _turn_id(prompt_id, 'prompt_id'), _turn_id(request_id, 'request_id')
        meta = json.loads(json.dumps(dict(meta or {})))     # JSON only; refuses anything else
        user_dir = self._user_dir(uid)
        out_dir = os.path.join(user_dir, 'out')
        os.makedirs(out_dir, exist_ok=True)
        tmp = os.path.join(out_dir, '.%s.tmp' % uuid.uuid4().hex)
        digest, size = hashlib.sha256(), 0
        try:
            with open(path, 'rb') as src, open(tmp, 'xb') as dst:
                for block in iter(lambda: src.read(_HASH_BLOCK), b''):
                    digest.update(block)
                    dst.write(block)
                    size += len(block)
                dst.flush()
                os.fsync(dst.fileno())
            if size == 0:
                raise ValueError('%s is empty' % path)
        except BaseException:
            _remove(tmp)
            raise
        xid = digest.hexdigest()
        if isinstance(meta.get('media'), dict):
            meta['media'].update(xid=xid, sha256=xid, size=size, mime=mime, prompt_id=pid,
                                 request_id=rid)
        stored_name = xid + _EXT.get(mime, '')
        final = os.path.join(out_dir, stored_name)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            doc['seq'] += 1
            rec = {'file': stored_name, 'name': name, 'mime': mime, 'size': size,
                   'purpose': purpose, 'seq': doc['seq'], 'created_at': self._clock(),
                   'meta': meta, 'prompt_id': pid, 'request_id': rid, 'consumed_at': None,
                   'replied': False, 'fetched': False, 'fetched_at': None}
            doc['files']['out'][xid] = rec
            self._save_index(user_dir, doc)
            if os.path.isfile(final):
                _remove(tmp)            # the same bytes are already there
            else:
                os.replace(tmp, final)
            return self._stored(user_dir, uid, 'out', xid, rec)

    def take_replied(self, user_id, *, prompt_id, request_id=None,
                     max_age_s: float = 1800) -> List[StoredFile]:
        """Outbound files for this turn that no reply has carried yet, made
        within ``max_age_s``; each is marked replied, so one reply carries
        it.  ``request_id=None`` takes any request of the prompt.

        Asked for every chat reply that has a prompt (core.chat_attachments
        .take_outbound), on every node: a user with no directory here has
        nothing to take, and is answered without a lock -- the lock registry
        is never evicted -- or an index read."""
        uid = _user_key(user_id)
        pid, rid = _turn_id(prompt_id, 'prompt_id'), _turn_id(request_id, 'request_id')
        user_dir = self._user_dir(uid)
        if not os.path.isdir(user_dir):
            return []
        with _lock_for(user_dir):
            now = self._clock()
            doc = self._load_index(user_dir, uid)
            taken = []
            for x, r in self._live(user_dir, doc, 'out'):
                if (r.get('prompt_id') == pid and (rid is None or r.get('request_id') == rid)
                        and not r['replied'] and now - r['created_at'] <= max_age_s):
                    r['replied'] = True
                    taken.append(self._stored(user_dir, uid, 'out', x, r))
            if taken:
                self._save_index(user_dir, doc)
            return taken

    def get_chunk(self, user_id, xid, i, *, chunk_size: Optional[int] = None) -> Dict[str, Any]:
        """Chunk ``i`` of an outbound file: ``file_chunk {xid, i, n, data,
        c, last}`` (``data`` raw bytes, ``c`` their sha256), or
        ``file_refused {reason: not_found | out_of_range}``.  Only this
        user's outbox is served, never an inbound file."""
        uid = _user_key(user_id)
        cs = self._chunk_size(chunk_size)
        if not _is_xid(xid):
            return _refused('not_found', None, i)
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            rec = self._held(user_dir, uid, xid, 'out')
        if rec is None:
            return _refused('not_found', xid, i)
        n = _n_chunks(rec['size'], cs)
        if not _is_int(i) or not 0 <= i < n:
            return _refused('out_of_range', xid, i)
        want = min(cs, rec['size'] - i * cs)
        try:
            with open(self._file_path(user_dir, 'out', rec), 'rb') as f:
                f.seek(i * cs)
                data = f.read(want)
        except OSError as e:
            logger.warning('user_file_store: could not read outbound %s: %s', xid[:12], e)
            return _refused('not_found', xid, i)
        if len(data) != want:
            logger.error('user_file_store: outbound %s is shorter than its record', xid[:12])
            return _refused('not_found', xid, i)
        return {'type': 'file_chunk', 'xid': xid, 'i': i, 'n': n, 'data': data,
                'c': hashlib.sha256(data).hexdigest(), 'last': i == n - 1}

    def list_outbox(self, user_id, *, prompt_id=None, unfetched_only=True) -> List[Dict[str, Any]]:
        """Outbound files as ``file_items`` entries, oldest first:
        ``{xid, name, mime, size, sha256, created_at, fetched, meta:
        {prompt_id, request_id[, media]}}``."""
        uid = _user_key(user_id)
        pid = _turn_id(prompt_id, 'prompt_id')
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            rows = self._live(user_dir, doc, 'out')
        items = []
        for x, r in rows:
            if (pid is not None and r.get('prompt_id') != pid) or (unfetched_only and r['fetched']):
                continue
            meta = {'prompt_id': r.get('prompt_id'), 'request_id': r.get('request_id')}
            if isinstance(r['meta'].get('media'), dict):
                meta['media'] = json.loads(json.dumps(r['meta']['media']))
            items.append({'xid': x, 'name': r['name'], 'mime': r['mime'], 'size': r['size'],
                          'sha256': x, 'created_at': r['created_at'], 'fetched': r['fetched'],
                          'meta': meta})
        return items

    def mark_fetched(self, user_id, xid) -> bool:
        """The phone has the outbound file.  True when it is in this user's
        outbox (again True on a repeat); the GC removes it
        outbox_after_fetch_s after the first fetch."""
        uid = _user_key(user_id)
        if not _is_xid(xid):
            return False
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            rec = doc['files']['out'].get(xid)
            if rec is None or not os.path.isfile(self._file_path(user_dir, 'out', rec)):
                return False
            if not rec['fetched']:
                rec.update(fetched=True, fetched_at=self._clock())
                self._save_index(user_dir, doc)
            return True

    # ── lookups ──

    def resolve(self, user_id, xid, *, box: Optional[str] = None) -> Optional[StoredFile]:
        """This user's committed file ``xid`` (inbox first, then outbox, or
        only ``box``), or None.  A record whose file is gone is None."""
        uid = _user_key(user_id)
        if box is not None and box not in BOXES:
            raise ValueError('box must be one of %s, got %r' % (BOXES, box))
        if not _is_xid(xid):
            return None
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            doc = self._load_index(user_dir, uid)
            for b in ((box,) if box else BOXES):
                rec = doc['files'][b].get(xid)
                if rec is not None and os.path.isfile(self._file_path(user_dir, b, rec)):
                    return self._stored(user_dir, uid, b, xid, rec)
        return None

    def usage(self, user_id) -> int:
        """Bytes this user holds: committed files plus the full size of each
        upload in progress (what the quota counts)."""
        uid = _user_key(user_id)
        user_dir = self._user_dir(uid)
        with _lock_for(user_dir):
            return self._usage(user_dir, self._load_index(user_dir, uid))

    # ── retention: the only GC ──

    def gc(self, *, now: Optional[float] = None) -> int:
        """Apply the retention windows to every user; returns files removed.

        Removes: uploads untouched for part_ttl_s; inbound clips and songs
        source_after_use_s after use, or source_unused_s after upload if never
        used; a logo once a newer logo exists; outbound files
        outbox_after_fetch_s after the fetch, or outbox_max_s after creation;
        and files no record or upload refers to once older than part_ttl_s
        (left by a crash).  A file another handle holds open stays for the
        next run.
        """
        now = self._clock() if now is None else now
        root = self.root
        try:
            entries = [e.path for e in os.scandir(root) if e.is_dir()]
        except FileNotFoundError:
            return 0
        removed = 0
        for user_dir in entries:
            with _lock_for(user_dir):
                try:
                    doc = self._load_index(user_dir, None)
                except OSError as e:
                    logger.error('user_file_store: GC skipped %s: %s', user_dir, e)
                    continue
                removed += self._gc_user(user_dir, doc, now)
        return removed

    def _gc_user(self, user_dir: str, doc: Dict[str, Any], now: float) -> int:
        """One user's GC pass over ``doc`` (caller holds the lock); saves
        the index when a record goes.  Returns files removed."""
        p = self.policy
        removed, changed = 0, False
        dkey = _dir_key(user_dir)
        live_parts = set()
        for part in self._parts(user_dir):
            if (dkey, part['xid']) in _COMMITTING or now - part['touched_at'] <= p['part_ttl_s']:
                live_parts.add(part['xid'])
            elif self._drop_part(user_dir, part['xid']):
                removed += 1
            else:
                live_parts.add(part['xid'])
        logos = [x for x, r in self._live(user_dir, doc, 'in') if r['purpose'] == 'logo']
        newest_logo = logos[-1] if logos else None
        for box in BOXES:
            records = doc['files'][box]
            for x in list(records):
                rec = records[x]
                path = self._file_path(user_dir, box, rec)
                if not os.path.isfile(path):
                    del records[x]
                    changed = True
                    continue
                if self._expired(box, x, rec, now, newest_logo):
                    if _remove(path):
                        del records[x]
                        removed += 1
                        changed = True
        removed += self._sweep_unreferenced(user_dir, doc, live_parts, now)
        if changed and doc.get('user_id'):
            self._save_index(user_dir, doc)
        return removed

    def _expired(self, box: str, xid: str, rec: Mapping, now: float,
                 newest_logo: Optional[str]) -> bool:
        p = self.policy
        if box == 'out':
            fetched_at = rec.get('fetched_at')
            return (now - rec['created_at'] > p['outbox_max_s']
                    or (rec['fetched'] and fetched_at is not None
                        and now - fetched_at > p['outbox_after_fetch_s']))
        if rec['purpose'] == 'logo':
            return xid != newest_logo
        used = rec.get('consumed_at')
        if used is not None:
            return now - used > p['source_after_use_s']
        return now - rec['created_at'] > p['source_unused_s']

    def _sweep_unreferenced(self, user_dir: str, doc: Mapping, live_parts: set,
                            now: float) -> int:
        """Remove files in in/, out/ and part/ that nothing refers to, once
        older than part_ttl_s (a young one may be a copy in progress)."""
        keep = {
            'in': {r['file'] for r in doc['files']['in'].values()},
            'out': {r['file'] for r in doc['files']['out'].values()},
            'part': {x + ext for x in live_parts for ext in ('.part', '.json')},
        }
        removed = 0
        for sub, names in keep.items():
            folder = os.path.join(user_dir, sub)
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                continue
            for entry in entries:
                if entry.name in names or not entry.is_file():
                    continue
                try:
                    old = now - entry.stat().st_mtime > self.policy['part_ttl_s']
                except OSError:
                    continue
                if old and _remove(entry.path):
                    removed += 1
        return removed


_STORE: Optional[UserFileStore] = None
_STORE_GUARD = threading.Lock()


def get_user_file_store() -> UserFileStore:
    """The process's store, on the default root."""
    global _STORE
    with _STORE_GUARD:
        if _STORE is None:
            _STORE = UserFileStore()
        return _STORE
