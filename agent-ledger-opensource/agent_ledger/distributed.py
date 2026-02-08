"""
Distributed Task Locking — ensures task uniqueness across regional hosts.

Uses Redis SET NX EX for atomic lock acquisition and Lua scripts for
atomic check-and-release. Prevents two agents from claiming the same task.

Usage:
    from agent_ledger.distributed import DistributedTaskLock

    lock = DistributedTaskLock(redis_client)

    if lock.try_claim_task("task_1", "agent_A"):
        # This agent owns the task
        ...
        lock.release_task("task_1", "agent_A")
"""

import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# Lua script for atomic check-and-delete (release only if owner matches)
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class DistributedTaskLock:
    """Redis-based distributed locking for task assignment."""

    LOCK_PREFIX = "agent_ledger:lock:"
    DEFAULT_TTL = 300  # 5 minutes auto-expire

    def __init__(self, redis_client):
        self._redis = redis_client

    def try_claim_task(self, task_id: str, agent_id: str, ttl: int = None) -> bool:
        """
        Atomically try to claim a task. Returns True if this agent got the lock.

        Uses Redis SET NX EX — atomic set-if-not-exists with expiry.
        """
        key = f"{self.LOCK_PREFIX}{task_id}"
        ttl = ttl or self.DEFAULT_TTL
        try:
            result = self._redis.set(key, agent_id, nx=True, ex=ttl)
            if result:
                logger.info(f"Task {task_id} claimed by {agent_id} (TTL={ttl}s)")
                return True
            else:
                owner = self._redis.get(key)
                logger.debug(f"Task {task_id} already claimed by {owner}")
                return False
        except Exception as e:
            logger.error(f"Failed to claim task {task_id}: {e}")
            return False

    def release_task(self, task_id: str, agent_id: str) -> bool:
        """
        Release a task lock (only if this agent owns it).

        Uses Lua script for atomic check-and-delete to prevent releasing
        another agent's lock.
        """
        key = f"{self.LOCK_PREFIX}{task_id}"
        try:
            result = self._redis.eval(_RELEASE_SCRIPT, 1, key, agent_id)
            if result:
                logger.info(f"Task {task_id} released by {agent_id}")
                return True
            else:
                logger.debug(f"Task {task_id} not owned by {agent_id}, cannot release")
                return False
        except Exception as e:
            logger.error(f"Failed to release task {task_id}: {e}")
            return False

    def get_task_owner(self, task_id: str) -> Optional[str]:
        """Get the agent that currently owns a task lock."""
        try:
            owner = self._redis.get(f"{self.LOCK_PREFIX}{task_id}")
            return owner
        except Exception:
            return None

    def is_task_locked(self, task_id: str) -> bool:
        """Check if a task is currently locked."""
        try:
            return self._redis.exists(f"{self.LOCK_PREFIX}{task_id}") > 0
        except Exception:
            return False

    def reclaim_stale_tasks(self, heartbeat, known_task_ids: List[str] = None) -> List[str]:
        """
        Find tasks locked by dead agents and release them.

        Cross-references lock owners against heartbeat data.
        Returns list of reclaimed task_ids.
        """
        reclaimed = []
        if not known_task_ids:
            # Scan for all lock keys (SCAN is non-blocking unlike KEYS)
            try:
                keys = list(self._redis.scan_iter(match=f"{self.LOCK_PREFIX}*", count=100))
                known_task_ids = [k.replace(self.LOCK_PREFIX, "") for k in keys]
            except Exception:
                return []

        for task_id in known_task_ids:
            owner = self.get_task_owner(task_id)
            if owner and not heartbeat.is_agent_alive(owner):
                # Owner is dead — release the lock
                key = f"{self.LOCK_PREFIX}{task_id}"
                try:
                    self._redis.delete(key)
                    reclaimed.append(task_id)
                    logger.info(f"Reclaimed task {task_id} from stale agent {owner}")
                except Exception as e:
                    logger.warning(f"Failed to reclaim task {task_id}: {e}")

        return reclaimed
