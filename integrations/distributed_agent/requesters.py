"""Who asked for a distributed goal, kept on THIS node.

A distributed goal's context goes to the coordinator ledger and, by gossip
(announce_goal, /tasks/available, /tasks/claim), to other people's nodes.
Under the owner's egress ruling (2026-09-26) a real user id must not travel
there.  Two tables stand in for it:

* distributed_submitters.json  goal id -> user id.  Who submitted a goal
  through POST /api/distributed/goals (it has no AgentGoal row), so
  /goals/<id>/progress can judge it (dashboard_service.may_steer).
* distributed_handles.json     an opaque, random per-goal handle -> user id.
  dispatch_goal_distributed puts the HANDLE in the context's ``user_id``
  slot instead of the user, and this node's id in ``source_node``.  A
  remote worker runs and reports under the handle (it has no such user
  anyway: worker_loop only uses the value as the /chat session key and
  world-model tag); the originating node maps it back to the person
  (resolve_requester) when it runs the task itself or writes the
  contribution notification (task_coordinator._notify_goal_contribution).

Where they live: the coordinator's own storage directory
(coordinator_backends.coordinator_storage_dir), beside its JSON ledger.  With
the JSON ledger the tables are shared exactly when the store is -- a remote
worker's submit_result lands on the node holding the ledger, and that node
holds the tables too.  They are node-local files on EVERY backend, so a Redis
ledger shared by several nodes splits from them: a worker on another node
finds no person for a handle it did not mint and sends no notification.

Both tables are bounded (the oldest record goes first; re-recording makes a
row the newest) and written atomically under one lock.
"""
import logging
import os
import secrets
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: A handle's prefix.  A context value without it is a raw id: a legacy
#: stamp, or a value a peer chose (resolve_requester).
HANDLE_PREFIX = 'req_'
_MAX_ROWS = 10000
_lock = threading.Lock()

#: Node ids that identify no node: two identity-less nodes would share them.
_NO_NODE = frozenset({'', 'unknown', 'none', 'null'})


def _dir() -> str:
    from .coordinator_backends import coordinator_storage_dir
    return coordinator_storage_dir()


def _path(name: str) -> str:
    return os.path.join(_dir(), name)


def _load(name: str) -> dict:
    from core.file_cache import cached_json_load
    try:
        return dict(cached_json_load(_path(name)) or {})
    except Exception:
        logger.debug('requester table %s unreadable', name, exc_info=True)
        return {}


def _put(name: str, key: str, value) -> None:
    """Set ``key`` in table ``name`` as its NEWEST row and write it back,
    evicting the oldest rows past _MAX_ROWS."""
    from core.file_cache import atomic_json_write
    table = _load(name)
    table.pop(str(key), None)          # re-recording moves it to the end
    table[str(key)] = value
    while len(table) > _MAX_ROWS:
        table.pop(next(iter(table)))
    os.makedirs(_dir(), exist_ok=True)
    atomic_json_write(_path(name), table)


def this_node_id() -> str:
    """This node's identity as the distributed stack names it
    (worker_loop._worker_node_id: the gossip node id, else HEVOLVE_NODE_ID),
    '' when it has none."""
    from .worker_loop import _worker_node_id
    return _worker_node_id() or ''


def is_this_node(source_node) -> bool:
    """True when ``source_node`` names this node, and names a real one."""
    source = str(source_node or '').strip()
    if source.lower() in _NO_NODE:
        return False
    here = this_node_id()
    return bool(here) and here.lower() not in _NO_NODE and source == here


# ── who submitted a goal (POST /api/distributed/goals) ───────────────────

def record_submitter(goal_id: str, user_id: str) -> None:
    with _lock:
        _put('distributed_submitters.json', goal_id, str(user_id))


def submitter_of(goal_id: str) -> Optional[str]:
    return _load('distributed_submitters.json').get(str(goal_id))


# ── the opaque handle a goal's context carries instead of its user ──────

def requester_handle(goal_id: str, user_id) -> Optional[str]:
    """The handle that stands for ``user_id`` on goal ``goal_id``; minted
    once per (goal, user) and reused, so a re-dispatch of the same goal
    carries the same handle.  None for no user."""
    if not user_id:
        return None
    user_id = str(user_id)
    with _lock:
        table = _load('distributed_handles.json')
        for handle, row in table.items():
            if (isinstance(row, dict) and row.get('goal') == str(goal_id)
                    and row.get('user') == user_id):
                return handle
        handle = HANDLE_PREFIX + secrets.token_hex(12)
        _put('distributed_handles.json', handle,
             {'goal': str(goal_id), 'user': user_id})
        return handle


def is_handle(value) -> bool:
    return bool(value) and str(value).startswith(HANDLE_PREFIX)


def resolve_requester(value, source_node) -> Optional[str]:
    """The person a context's ``user_id`` value stands for on THIS node, or
    None when there is nobody here to act for.

    * a handle this node minted -> its user; another node's handle -> None;
    * a raw id -> itself ONLY when ``source_node`` is this node (a legacy
      stamp of our own).  Review of e9daad6c5: returned unchanged, a peer
      could name a local user outright and a pulled task ran /chat as them.
      A peer's announce cannot claim this node either: ingest drops the
      requester and the source (peer_view).
    """
    if not value:
        return None
    value = str(value)
    if is_handle(value):
        row = _load('distributed_handles.json').get(value)
        return row.get('user') if isinstance(row, dict) else None
    return value if is_this_node(source_node) else None


# ── what other nodes see of a context ───────────────────────────────────

#: Context keys that say who asked and from where.  Never served to a peer,
#: and never taken from one.
REQUESTER_KEYS = ('user_id', 'source_node')


def peer_view(context) -> dict:
    """``context`` without the requester: what crosses between nodes, in
    either direction.

    * served: /tasks/available and /tasks/claim served the whole task
      context, user id included (review of e9daad6c5: a legacy REAL-USER id
      was served).  The source node goes too; it matters only to the node
      that stamped it.
    * taken: a context a peer announces is kept without them, so nothing in
      this ledger can claim a local user, or this node as its source, on a
      peer's word (resolve_requester trusts a raw id only from this node).
    """
    return {k: v for k, v in (context or {}).items()
            if k not in REQUESTER_KEYS}
