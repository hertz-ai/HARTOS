"""Who asked for a distributed goal, kept on THIS node.

A distributed goal's context goes to the shared coordinator ledger and, by
gossip (announce_goal), to other people's nodes.  Under the owner's egress
ruling (2026-09-26) a real user id must not travel there.  Two node-local
tables stand in for it, both under agent_data/:

* distributed_submitters.json  goal id -> user id.  Who submitted a goal
  through POST /api/distributed/goals (it has no AgentGoal row), so
  /goals/<id>/progress can judge it (dashboard_service.may_steer).
* distributed_handles.json     an opaque, random per-goal handle -> user id.
  dispatch_goal_distributed puts the HANDLE in the context's ``user_id``
  slot instead of the user.  A remote worker runs and reports under the
  handle (it has no such user anyway: worker_loop only uses the value as the
  /chat session key and world-model tag); the originating node maps it back
  to the person (resolve_requester) when it runs the task itself or writes
  the contribution notification (task_coordinator._notify_goal_contribution).

Both tables are bounded (oldest entries go first) and written atomically
under one lock.
"""
import logging
import os
import secrets
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: A handle's prefix; a context value without it is a legacy id or a machine
#: label and passes through resolve_requester unchanged.
HANDLE_PREFIX = 'req_'
_MAX_ROWS = 10000
_lock = threading.Lock()


def _dir() -> str:
    from core.platform_paths import get_agent_data_dir
    return get_agent_data_dir()


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
    """Set ``key`` in table ``name`` and write it back, bounded."""
    from core.file_cache import atomic_json_write
    table = _load(name)
    table[str(key)] = value
    while len(table) > _MAX_ROWS:
        table.pop(next(iter(table)))
    os.makedirs(_dir(), exist_ok=True)
    atomic_json_write(_path(name), table)


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


def resolve_requester(value) -> Optional[str]:
    """The person a context's ``user_id`` value stands for on THIS node.

    A handle this node minted -> its user; a handle it did not (another
    node's) -> None, there is nobody here to act for; anything else (a
    legacy id, a machine label) -> unchanged.
    """
    if not value:
        return None
    value = str(value)
    if not value.startswith(HANDLE_PREFIX):
        return value
    row = _load('distributed_handles.json').get(value)
    return row.get('user') if isinstance(row, dict) else None
