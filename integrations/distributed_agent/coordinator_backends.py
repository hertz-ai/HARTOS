"""
Coordinator Backend Abstraction — pluggable task coordination without Redis.

Three backends for DistributedTaskCoordinator:
1. Redis       — fast pub/sub, shared queue across nodes (production multi-node)
2. In-Memory   — thread-safe, single-process (default when Redis unavailable)
3. Gossip      — HTTP-based peer gossip for multi-node without Redis

The coordinator doesn't know or care which backend it uses. All three
provide SmartLedger + TaskLock + TaskVerification + TaskBaseline.

Philosophy: Redis is ONE transport, not THE transport. Distribution is
emergent from having peers. A single-node hive works with in-memory.
A multi-node hive can use Redis OR peer gossip. Every drop counts.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve_social')


# ═══════════════════════════════════════════════════════════════
# In-Memory Task Lock (replaces DistributedTaskLock when no Redis)
# ═══════════════════════════════════════════════════════════════

class InMemoryTaskLock:
    """Thread-safe in-process task lock. Same interface as DistributedTaskLock.

    Uses a threading.Lock for atomic claim/release. TTL enforced via
    background expiry check. Sufficient for single-node or testing.
    """

    LOCK_PREFIX = "agent_ledger:lock:"
    DEFAULT_TTL = 300

    def __init__(self):
        self._locks: Dict[str, Dict[str, Any]] = {}  # task_id → {agent_id, expires_at}
        self._mu = threading.Lock()

    def try_claim_task(self, task_id: str, agent_id: str, ttl: int = None) -> bool:
        ttl = ttl if ttl is not None else self.DEFAULT_TTL
        now = time.time()
        with self._mu:
            existing = self._locks.get(task_id)
            if existing and existing['expires_at'] > now:
                return False  # Already claimed and not expired
            self._locks[task_id] = {
                'agent_id': agent_id,
                'expires_at': now + ttl,
            }
            return True

    def release_task(self, task_id: str, agent_id: str) -> bool:
        with self._mu:
            existing = self._locks.get(task_id)
            if existing and existing['agent_id'] == agent_id:
                del self._locks[task_id]
                return True
            return False

    def get_task_owner(self, task_id: str) -> Optional[str]:
        with self._mu:
            entry = self._locks.get(task_id)
            if entry and entry['expires_at'] > time.time():
                return entry['agent_id']
            return None

    def is_task_locked(self, task_id: str) -> bool:
        return self.get_task_owner(task_id) is not None

    def reclaim_stale_tasks(self, heartbeat=None, known_task_ids: List[str] = None) -> List[str]:
        reclaimed = []
        now = time.time()
        with self._mu:
            expired = [tid for tid, v in self._locks.items() if v['expires_at'] <= now]
            for tid in expired:
                del self._locks[tid]
                reclaimed.append(tid)
        return reclaimed


# ═══════════════════════════════════════════════════════════════
# In-Memory Host Registry (replaces RegionalHostRegistry when no Redis)
# ═══════════════════════════════════════════════════════════════

class InMemoryHostRegistry:
    """Thread-safe in-process host registry. Same interface as RegionalHostRegistry."""

    def __init__(self, host_id: str, host_url: str = ""):
        self.host_id = host_id
        self.host_url = host_url
        self._hosts: Dict[str, Dict[str, Any]] = {}
        self._mu = threading.Lock()

    def register_host(self, capabilities: List[str],
                      compute_budget: Optional[Dict[str, Any]] = None,
                      agent_ids: Optional[List[str]] = None) -> bool:
        with self._mu:
            self._hosts[self.host_id] = {
                "host_id": self.host_id,
                "host_url": self.host_url,
                "capabilities": capabilities,
                "compute_budget": compute_budget or {},
                "agent_ids": agent_ids or [],
                "registered_at": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            }
        return True

    def deregister_host(self) -> bool:
        with self._mu:
            self._hosts.pop(self.host_id, None)
        return True

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        with self._mu:
            return list(self._hosts.values())

    def get_hosts_with_capability(self, capability: str) -> List[Dict[str, Any]]:
        return [h for h in self.get_all_hosts()
                if capability in h.get("capabilities", [])]

    def get_host_info(self, host_id: str) -> Optional[Dict[str, Any]]:
        with self._mu:
            return self._hosts.get(host_id)

    def update_compute_usage(self, usage: Dict[str, Any]) -> None:
        with self._mu:
            if self.host_id in self._hosts:
                self._hosts[self.host_id]["compute_usage"] = usage
                self._hosts[self.host_id]["last_seen"] = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════
# Gossip Task Bridge — multi-node without Redis
# ═══════════════════════════════════════════════════════════════

class GossipTaskBridge:
    """Propagate tasks to peers via existing HTTP gossip protocol.

    When a node submits a goal, the bridge announces it to known peers
    via POST /api/distributed/tasks/announce. Peers add the tasks to
    their local coordinator and can claim them.

    This is pull-based: peers poll each other's task lists via gossip
    exchange rounds, not push-based pub/sub.
    """

    def __init__(self):
        self._announced: Dict[str, float] = {}  # goal_id → timestamp
        self._mu = threading.Lock()

    def announce_goal(self, goal_id: str, objective: str,
                      tasks: List[Dict], context: Dict) -> int:
        """Announce a new goal to all known peers via HTTP POST.

        If the peer has an X25519 public key, the payload is E2E encrypted
        so neither network observers nor the hosting node see the task data.
        Falls back to plaintext for peers without X25519 keys (old nodes).

        Returns number of peers notified.
        """
        notified = 0
        peers = self._get_active_peers()

        payload = {
            'goal_id': goal_id,
            'objective': objective,
            'tasks': tasks,
            'context': context,
        }

        for peer in peers:
            peer_url = peer.get('host_url') or peer.get('url', '')
            if not peer_url:
                continue
            try:
                import requests
                send_payload = payload
                peer_x25519 = peer.get('x25519_public', '')
                if peer_x25519:
                    try:
                        from security.channel_encryption import encrypt_json_for_peer
                        send_payload = {'encrypted': True,
                                        'envelope': encrypt_json_for_peer(payload, peer_x25519)}
                    except Exception:
                        pass  # Encryption unavailable, send plaintext
                resp = requests.post(
                    f'{peer_url}/api/distributed/tasks/announce',
                    json=send_payload,
                    timeout=5,
                )
                if resp.status_code == 200:
                    notified += 1
            except Exception:
                pass

        with self._mu:
            self._announced[goal_id] = time.time()

        logger.debug(f"Gossip: announced goal {goal_id} to {notified}/{len(peers)} peers")
        return notified

    def pull_tasks_from_peers(self) -> List[Dict]:
        """Pull unclaimed tasks from known peers (gossip exchange).

        If the response is E2E encrypted, decrypts it using our X25519 key.
        """
        tasks = []
        peers = self._get_active_peers()

        for peer in peers:
            peer_url = peer.get('host_url') or peer.get('url', '')
            if not peer_url:
                continue
            try:
                import requests
                resp = requests.get(
                    f'{peer_url}/api/distributed/tasks/available',
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Handle E2E encrypted response
                    if data.get('encrypted') and data.get('envelope'):
                        try:
                            from security.channel_encryption import decrypt_json_from_peer
                            decrypted = decrypt_json_from_peer(data['envelope'])
                            if decrypted:
                                tasks.extend(decrypted.get('tasks', []))
                                continue
                        except Exception:
                            pass  # Decryption failed, try plaintext
                    tasks.extend(data.get('tasks', []))
            except Exception:
                pass

        return tasks

    @staticmethod
    def _get_active_peers() -> List[Dict]:
        """Get active peers from the PeerNode table.

        Includes x25519_public for E2E encryption of task payloads.
        """
        try:
            from integrations.social.models import get_db, PeerNode
            db = get_db()
            try:
                peers = db.query(PeerNode).filter(
                    PeerNode.status == 'active'
                ).all()
                return [{'host_url': p.url, 'node_id': p.node_id,
                         'x25519_public': getattr(p, 'x25519_public', '') or ''}
                        for p in peers if p.url]
            finally:
                db.close()
        except Exception:
            return []


# ═══════════════════════════════════════════════════════════════
# Backend Factory — single function to create the best available backend
# ═══════════════════════════════════════════════════════════════

def create_coordinator(agent_id: str = None):
    """Create a DistributedTaskCoordinator with the best available backend.

    Priority:
    1. Redis (if reachable) — shared across nodes, pub/sub, distributed locks
    2. In-memory (fallback) — single-node, thread-safe, no external deps

    Gossip bridge is always attached when peers exist, regardless of backend.
    This allows in-memory nodes to announce tasks to peers.

    Returns:
        (coordinator, backend_type) tuple, or (None, None) if creation fails
    """
    agent_id = agent_id or os.environ.get('HEVOLVE_AGENT_ID', 'local')

    # In bundled/desktop mode, Redis is never available — skip the attempt
    # entirely to avoid a 1-30s timeout stall on every startup.
    if not os.environ.get('NUNBA_BUNDLED'):
        coordinator = _try_redis_backend(agent_id)
        if coordinator:
            return coordinator, 'redis'

    # Fall back to in-memory + JSON file backend
    coordinator = _create_inmemory_backend(agent_id)
    if coordinator:
        return coordinator, 'inmemory'

    return None, None


def _try_redis_backend(agent_id: str):
    """Try to create coordinator with Redis backend."""
    try:
        import redis
        host = os.environ.get('REDIS_HOST', 'localhost')
        port = int(os.environ.get('REDIS_PORT', 6379))

        # Quick connectivity check — fail fast, no retries
        r = redis.Redis(host=host, port=port, decode_responses=True,
                        socket_connect_timeout=1, socket_timeout=1,
                        retry_on_timeout=False)
        r.ping()

        from agent_ledger import SmartLedger, RedisBackend
        from agent_ledger.distributed import DistributedTaskLock
        from agent_ledger.verification import TaskVerification, TaskBaseline

        backend = RedisBackend(host=host, port=port)
        shared_redis = backend.redis_client

        ledger = SmartLedger(
            agent_id=agent_id,
            session_id='distributed',
            backend=backend,
        )
        ledger.enable_pubsub(shared_redis)

        from .task_coordinator import DistributedTaskCoordinator
        coordinator = DistributedTaskCoordinator(
            ledger=ledger,
            task_lock=DistributedTaskLock(shared_redis),
            verifier=TaskVerification(shared_redis),
            baseline=TaskBaseline(backend),
        )
        logger.info("Distributed coordinator: Redis backend active")
        return coordinator

    except Exception as e:
        logger.debug(f"Redis backend unavailable: {e}")
        return None


def _create_inmemory_backend(agent_id: str):
    """Create coordinator with in-memory/JSON backend (no Redis needed)."""
    try:
        from agent_ledger import SmartLedger, JSONBackend
        from agent_ledger.verification import TaskVerification, TaskBaseline

        # Use agent_data directory for JSON persistence (must be absolute
        # and writable — never use relative paths, which resolve to the
        # read-only install dir in bundled mode).
        db_path = os.environ.get('HEVOLVE_DB_PATH', '')
        if db_path and db_path != ':memory:' and os.path.isabs(db_path):
            storage_dir = os.path.join(os.path.dirname(db_path), 'distributed_tasks')
        else:
            # Always fall back to user-writable data dir
            try:
                from core.platform_paths import get_agent_data_dir
                storage_dir = os.path.join(get_agent_data_dir(), 'distributed_tasks')
            except ImportError:
                storage_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data', 'distributed_tasks')
        os.makedirs(storage_dir, exist_ok=True)

        backend = JSONBackend(storage_dir=storage_dir)
        ledger = SmartLedger(
            agent_id=agent_id,
            session_id='distributed',
            backend=backend,
        )

        from .task_coordinator import DistributedTaskCoordinator
        coordinator = DistributedTaskCoordinator(
            ledger=ledger,
            task_lock=InMemoryTaskLock(),
            verifier=TaskVerification(),  # In-memory verification
            baseline=TaskBaseline(),      # In-memory baselines
        )
        logger.info("Distributed coordinator: in-memory backend active (no Redis)")
        return coordinator

    except Exception as e:
        logger.warning(f"In-memory backend creation failed: {e}")
        return None
