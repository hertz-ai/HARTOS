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
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lua script for atomic check-and-delete (release only if owner matches)
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script for atomic check-and-renew (EXPIRE only if owner matches).
# Prevents one agent from accidentally extending another agent's lock,
# which would defeat the stale-reclaim guarantee in reclaim_stale_tasks.
_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class DistributedTaskLock:
    """Redis-based distributed locking for task assignment."""

    LOCK_PREFIX = "agent_ledger:lock:"
    DEFAULT_TTL = 300  # 5 minutes auto-expire

    # Heartbeat cadence — renew 3x more often than TTL expiry so two
    # missed renewals still leave one cycle of safety margin before the
    # lock disappears.  90s for a 300s TTL = fires at t=90, 180, 270.
    HEARTBEAT_INTERVAL = 90  # seconds

    def __init__(self, redis_client):
        self._redis = redis_client
        # (task_id, agent_id) -> (threading.Event stop-signal, ttl).  Used
        # to track which locks have an active heartbeat so renew_all()
        # can iterate and stop_heartbeat() can tear one down cleanly.
        self._heartbeats: Dict[Tuple[str, str], Tuple[threading.Event, int]] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()

    def try_claim_task(self, task_id: str, agent_id: str, ttl: int = None,
                       heartbeat: bool = False) -> bool:
        """
        Atomically try to claim a task. Returns True if this agent got the lock.

        Uses Redis SET NX EX — atomic set-if-not-exists with expiry.

        Args:
            heartbeat: When True, automatically start a background renew
                loop so the lock survives tasks that run longer than ttl.
                The caller is responsible for calling release_task or
                stop_heartbeat when done (release_task does both).
        """
        key = f"{self.LOCK_PREFIX}{task_id}"
        ttl = ttl or self.DEFAULT_TTL
        try:
            result = self._redis.set(key, agent_id, nx=True, ex=ttl)
            if result:
                logger.info(f"Task {task_id} claimed by {agent_id} (TTL={ttl}s)")
                if heartbeat:
                    self.start_heartbeat(task_id, agent_id, ttl=ttl)
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
        another agent's lock.  Also stops any active heartbeat for this
        (task_id, agent_id) pair so the renew loop doesn't keep running
        after the lock is gone.
        """
        self.stop_heartbeat(task_id, agent_id)
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

    def renew(self, task_id: str, agent_id: str, ttl: int = None) -> bool:
        """Extend the TTL on a lock we already own.

        Returns True when Redis confirmed the EXPIRE (lock is ours and
        still present), False otherwise.  Uses a Lua check-and-renew so
        a racing release/reclaim can't hand the lock to another agent
        while we're extending it.

        Callers that hold the lock for longer than DEFAULT_TTL must
        either call renew() periodically themselves or use
        ``start_heartbeat`` which runs a background thread.
        """
        key = f"{self.LOCK_PREFIX}{task_id}"
        ttl = ttl or self.DEFAULT_TTL
        try:
            result = self._redis.eval(_RENEW_SCRIPT, 1, key, agent_id, ttl)
            if result:
                logger.debug(f"Task {task_id} lock renewed by {agent_id} (TTL={ttl}s)")
                return True
            logger.warning(
                f"Task {task_id} lock renewal failed: not owned by {agent_id} "
                f"(lost lock or never claimed)")
            return False
        except Exception as e:
            logger.error(f"Failed to renew task {task_id}: {e}")
            return False

    def start_heartbeat(self, task_id: str, agent_id: str,
                        ttl: int = None) -> bool:
        """Begin renewing this lock in the background every HEARTBEAT_INTERVAL.

        One shared thread services all heartbeats so we don't spawn a
        thread per claimed task.  Safe to call multiple times for the
        same (task_id, agent_id) — subsequent calls are no-ops.
        """
        ttl = ttl or self.DEFAULT_TTL
        key = (task_id, agent_id)
        with self._heartbeat_lock:
            if key in self._heartbeats:
                return False
            self._heartbeats[key] = (threading.Event(), ttl)
            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_stop = threading.Event()
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop,
                    name='distributed-lock-heartbeat',
                    daemon=True,
                )
                self._heartbeat_thread.start()
        return True

    def stop_heartbeat(self, task_id: str, agent_id: str) -> bool:
        """Stop renewing this lock.  Safe to call even if no heartbeat
        is active for this pair.  When the last heartbeat is removed,
        the shared renew thread exits gracefully.
        """
        key = (task_id, agent_id)
        with self._heartbeat_lock:
            entry = self._heartbeats.pop(key, None)
            if entry is None:
                return False
            entry[0].set()
            if not self._heartbeats:
                # No more heartbeats → signal loop to exit.  Joining is
                # left to stop_all() since callers of release_task are
                # usually on a hot path.
                self._heartbeat_stop.set()
        return True

    def stop_all_heartbeats(self, timeout: float = 5.0) -> None:
        """Stop every active heartbeat and join the renew thread.  Used
        by integration tests and graceful process shutdown.
        """
        with self._heartbeat_lock:
            for entry in list(self._heartbeats.values()):
                entry[0].set()
            self._heartbeats.clear()
            self._heartbeat_stop.set()
            thread = self._heartbeat_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _heartbeat_loop(self) -> None:
        """Background: renew every claimed lock every HEARTBEAT_INTERVAL."""
        while not self._heartbeat_stop.is_set():
            # Wait interval (interruptible).  Break immediately on stop
            # so shutdown doesn't hang for up to HEARTBEAT_INTERVAL.
            if self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
                break
            with self._heartbeat_lock:
                snapshot = list(self._heartbeats.items())
            for (task_id, agent_id), (stop_event, ttl) in snapshot:
                if stop_event.is_set():
                    continue
                try:
                    ok = self.renew(task_id, agent_id, ttl=ttl)
                    if not ok:
                        # We lost the lock (reclaimed as stale or Redis
                        # dropped it).  Remove from tracking so we don't
                        # keep spamming renew attempts.
                        with self._heartbeat_lock:
                            self._heartbeats.pop((task_id, agent_id), None)
                except Exception as e:
                    logger.debug(
                        f"Heartbeat renew failed for {task_id} / {agent_id}: {e}")

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
        Returns list of reclaimed task_ids.  Redis ConnectionError is
        trapped and logged — caller's outer loop should apply backoff
        (see DistributedWorkerLoop for the reference pattern).
        """
        # Resolve redis ConnectionError lazily — distributed.py should
        # remain importable even when the redis package isn't installed
        # (core module, used by tests with fake redis clients).
        try:
            from redis.exceptions import ConnectionError as RedisConnectionError
        except Exception:  # pragma: no cover
            RedisConnectionError = ConnectionError  # type: ignore[misc, assignment]

        reclaimed = []
        if not known_task_ids:
            # Scan for all lock keys (SCAN is non-blocking unlike KEYS)
            try:
                keys = list(self._redis.scan_iter(match=f"{self.LOCK_PREFIX}*", count=100))
                known_task_ids = [k.replace(self.LOCK_PREFIX, "") for k in keys]
            except RedisConnectionError as e:
                logger.warning(f"reclaim_stale_tasks: Redis ConnectionError during scan: {e}")
                return []
            except Exception:
                return []

        for task_id in known_task_ids:
            try:
                owner = self.get_task_owner(task_id)
            except RedisConnectionError as e:
                logger.warning(f"reclaim_stale_tasks: Redis down, aborting reclaim: {e}")
                return reclaimed
            if owner and not heartbeat.is_agent_alive(owner):
                # Owner is dead — release the lock
                key = f"{self.LOCK_PREFIX}{task_id}"
                try:
                    self._redis.delete(key)
                    reclaimed.append(task_id)
                    logger.info(f"Reclaimed task {task_id} from stale agent {owner}")
                except RedisConnectionError as e:
                    logger.warning(f"reclaim_stale_tasks: Redis down mid-loop: {e}")
                    return reclaimed
                except Exception as e:
                    logger.warning(f"Failed to reclaim task {task_id}: {e}")

        return reclaimed
