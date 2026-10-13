"""core.peer_link.file_channel -- a person's own phone moving files to and
from this desktop, over its device link.

Owner ruling U3 (2026-10-13), option A: a second device door beside the
mobile adapter's api_request, on its own PeerLink channel ('file', 0x0C, in
core.peer_link.channels) and with no HTTP ingress; the 'tunnel' channel is
the precedent.  Her phone uploads the clips, the logo and the song audio a
reel is made from, and pulls the finished reel back.  The bytes live in
core.user_file_store, the one store: this module reads a frame, asks the
store, and answers.  It keeps no file state of its own.

Only a person's own phone is answered: a device link with its user
(PeerLinkManager.get_device_link).  Everything is that user's: the store keys
every file by the link's user and the file's sha256, so a phone reaches only
its own person's files, whatever a frame names.  Another node's frames, and
anything that is not a dict, are not answered.  Every frame is a request
(link.py runs each on its own thread, at most 8 at once per link, and
answers a phone past that with {'type': 'busy'}):

  file_offer   {name, size, sha256, mime, purpose, captured_at?, duration_ms?}
                 -> file_accept {xid, chunk_size, n, missing, max_inflight}
                    | file_have {xid} | file_refused {reason[, xid]}
  file_put     {xid, i, b64, c} -> file_ack {xid, i} | file_refused {reason[, xid], i}
  file_commit  {xid}            -> file_done {xid} | file_corrupt {xid, missing}
                                   | file_refused {reason[, xid]}
  file_abort   {xid}            -> file_aborted {xid} | file_refused {reason[, xid]}
  file_get     {xid, i}         -> file_chunk {xid, i, n, chunk_size, b64, c, last}
                                   | file_refused {reason[, xid], i}
  file_list    {prompt_id?}     -> file_items {items} | file_refused {bad_request}
  file_fetched {xid}            -> file_ack {xid} | file_refused {not_found}

``xid`` is the file's sha256; ``missing`` holds inclusive [first, last] chunk
ranges; ``b64`` is standard base64 and ``c`` the raw chunk's sha256; chunk i
starts at byte i * chunk_size.  Of an offer frame the store keeps only the
capture facts (captured_at, duration_ms; amendment A1).  Asking again is
always safe: a chunk put twice, a commit or a fetched repeated, the same
offer made again (it resumes) each answer as the first did, so a frame the
relay lost is a timeout the phone simply retries.

Chunk sizes.  An upload's chunks are the store's (1 MiB) on a direct link,
and RELAY_CHUNK on a link that crosses the relay, which base64s every frame
once more under an 8 MiB cap and drops frames past its queue; a phone that
moves from one to the other offers again, and the store keeps the bytes
already written.  A download's chunks are RELAY_CHUNK on every link: the
phone resumes a download from its own byte count, so chunk i must be the
same bytes whichever link it asks on.  Each file_chunk names its chunk_size.

Refusals are the store's (core.user_file_store.REFUSALS) and two the door
makes itself: 'bad_request' (a file_list prompt_id that is neither a string
nor an int) and 'error' (this desktop failed; why is in its log).  A full
disk the store did not catch itself is 'no_space'.  A refusal the door makes
names no xid; on file_put and file_get it names the frame's i.  REFUSALS
lists them all, and tests/fixtures/peerlink_file_vector.json pins them with
the frames for the phone (DesktopFilesVectorTest).

install() binds the handler, named in this node's handshake so a phone asks
only a desktop that answers, and applies the store's retention once.
hartos/hartos_bootstrap.py installs it on every node but central.  core
imports no integrations: this needs only the link manager and the store.
"""
import base64
import binascii
import errno
import logging
from typing import Any, Callable, Dict, Optional

from core.peer_link.channels import (
    FILE_ABORT, FILE_COMMIT, FILE_FETCHED, FILE_GET, FILE_LIST, FILE_OFFER, FILE_PUT,
    device_send_types)
from core.peer_link.link_manager import get_link_manager
from core.user_file_store import REFUSALS as STORE_REFUSALS
from core.user_file_store import get_user_file_store

logger = logging.getLogger('hevolve.peer_link')

CHANNEL = 'file'
#: The device requests this door answers: every type the channel opens to a
#: phone (the handshake's device_requests names them).
ANSWERS = device_send_types(CHANNEL)
#: An upload's chunk on a relayed link, and every download's chunk: 256 KiB
#: stays far under the relay's 8 MiB frame cap after its second base64 layer,
#: and a lost frame costs a phone one small retry.
RELAY_CHUNK = 256 << 10
#: The refusals the door makes itself (the module docstring says when).
DOOR_REFUSALS = ('bad_request', 'error')
#: Every reason a file_refused on this channel can carry.
REFUSALS = tuple(STORE_REFUSALS) + DOOR_REFUSALS


def _store():
    """The process's store (tests point this at a store on a temp root)."""
    return get_user_file_store()


def _refused(reason: str, frame: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A refusal the door makes itself: on a chunk request it names the
    frame's i, as the store's own refusals do."""
    reply = {'type': 'file_refused', 'reason': reason}
    if frame is not None and frame.get('type') in (FILE_PUT, FILE_GET) and 'i' in frame:
        reply['i'] = frame['i']
    return reply


def _upload_chunk(store, link) -> int:
    chunk = store.policy['chunk_size']
    return min(chunk, RELAY_CHUNK) if link.requires_e2e else chunk


def _download_chunk(store) -> int:
    return min(store.policy['chunk_size'], RELAY_CHUNK)


# ── one answer per request type: (store, the link's user, link, frame) ───────

def _offer(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    return store.offer(user, frame.get('name'), frame.get('size'), frame.get('sha256'),
                       frame.get('mime'), frame.get('purpose'), meta=frame,
                       chunk_size=_upload_chunk(store, link))


def _put(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = base64.b64decode(frame.get('b64') or '', validate=True)
    except (binascii.Error, TypeError, ValueError):
        data = None     # the store refuses it 'chunk_corrupt', after its own checks
    return store.put_chunk(user, frame.get('xid'), frame.get('i'), data, frame.get('c'))


def _commit(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    return store.commit(user, frame.get('xid'))


def _abort(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    return store.abort(user, frame.get('xid'))


def _get(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    chunk = _download_chunk(store)
    reply = store.get_chunk(user, frame.get('xid'), frame.get('i'), chunk_size=chunk)
    if reply.get('type') == 'file_chunk':
        reply['b64'] = base64.b64encode(reply.pop('data')).decode('ascii')
        reply['chunk_size'] = chunk
    return reply


def _list(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    try:
        items = store.list_outbox(user, prompt_id=frame.get('prompt_id'))
    except ValueError:
        return _refused('bad_request')
    return {'type': 'file_items', 'items': items}


def _fetched(store, user: str, link, frame: Dict[str, Any]) -> Dict[str, Any]:
    if store.mark_fetched(user, frame.get('xid')):
        return {'type': 'file_ack', 'xid': frame['xid']}
    return _refused('not_found')


_ANSWER: Dict[str, Callable[..., Dict[str, Any]]] = {
    FILE_OFFER: _offer, FILE_PUT: _put, FILE_COMMIT: _commit, FILE_ABORT: _abort,
    FILE_GET: _get, FILE_LIST: _list, FILE_FETCHED: _fetched,
}

#: Requests a phone sends many of: logged at debug, the rest at info.
_CHATTY = (FILE_PUT, FILE_GET, FILE_LIST)


def _device_link(peer_id: str):
    """The live link to the person's own phone behind ``peer_id``, or None (a
    node's link, or none): only a phone's frames on 'file' are answered."""
    try:
        return get_link_manager().get_device_link(peer_id)
    except Exception as e:
        logger.warning("A 'file' frame from %s is unanswered: link lookup failed: %s",
                       peer_id, e)
        return None


def _log(peer_id: str, kind: str, frame: Dict[str, Any], reply: Dict[str, Any]) -> None:
    """The request's shape and outcome only: never a file's name or bytes."""
    xid = frame.get('xid') if kind != FILE_OFFER else frame.get('sha256')
    xid = xid[:12] if isinstance(xid, str) else '-'
    outcome = reply.get('reason') or reply.get('type')
    log = logger.debug if kind in _CHATTY else logger.info
    if kind == FILE_OFFER:
        log("Device %s %s %s (%s bytes, %s) -> %s", peer_id, kind, xid,
            frame.get('size'), frame.get('purpose'), outcome)
    else:
        log("Device %s %s %s -> %s", peer_id, kind, xid, outcome)


def handle_file_frame(channel: str, data: Any, peer_id: str) -> Optional[dict]:
    """The 'file' handler: a person's own phone's request, answered from the
    store.  None (not answered) for anything else."""
    if not isinstance(data, dict):
        return None
    kind = data.get('type')
    answer = _ANSWER.get(kind) if isinstance(kind, str) else None
    if answer is None:
        return None
    link = _device_link(peer_id)
    if link is None:
        return None
    try:
        reply = answer(_store(), link.user_id, link, data)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            logger.warning("Device %s %s: this desktop's disk is full: %s", peer_id, kind, e)
            return _refused('no_space', data)
        logger.warning("Device %s %s failed on this desktop: %s", peer_id, kind, e,
                       exc_info=True)
        return _refused('error', data)
    except Exception as e:
        logger.warning("Device %s %s failed on this desktop: %s", peer_id, kind, e,
                       exc_info=True)
        return _refused('error', data)
    _log(peer_id, kind, data, reply)
    return reply


def install() -> bool:
    """Answer a person's own phones' file requests on 'file', named in this
    node's handshake (device_requests), and apply the store's retention once
    (core.user_file_store.UserFileStore.gc): the store sweeps one person on
    each of that person's offers, so this is when everyone else's expired
    uploads, used clips and fetched reels go.  False when the handler could
    not be bound."""
    try:
        get_link_manager().register_channel_handler(CHANNEL, handle_file_frame,
                                                    answers=ANSWERS)
    except Exception as e:
        logger.warning("File channel not installed: %s", e)
        return False
    try:
        removed = _store().gc()
        if removed:
            logger.info("File channel: the store's retention removed %d file(s)", removed)
    except Exception as e:
        logger.warning("File channel: the store's retention pass failed: %s", e)
    return True
