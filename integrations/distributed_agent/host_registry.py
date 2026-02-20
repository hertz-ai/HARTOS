"""
Regional Host Registry — tracks which hosts contribute compute.

Stores host info in Redis hashes so all distributed agents can discover
each other. Reuses the same Redis connection used by RedisBackend / heartbeat.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Hosts not seen within this window (seconds) are considered stale and purged.
STALE_THRESHOLD = 300  # 5 minutes


class RegionalHostRegistry:
    """
    Tracks regional hosts that contribute compute to distributed coding.

    Each host registers with capabilities (tools, skills) and compute budget.
    Other hosts discover peers via shared Redis.

    Host entries carry a ``last_seen`` timestamp.  Entries older than
    ``STALE_THRESHOLD`` seconds are automatically purged when the host list
    is queried via ``get_all_hosts()`` or ``get_hosts_with_capability()``.
    """

    HOSTS_HASH = "distributed_agent:hosts"
    HOST_PREFIX = "distributed_agent:host:"

    def __init__(self, redis_client, host_id: str, host_url: str = ""):
        self._redis = redis_client
        self.host_id = host_id
        self.host_url = host_url

    def register_host(
        self,
        capabilities: List[str],
        compute_budget: Optional[Dict[str, Any]] = None,
        agent_ids: Optional[List[str]] = None,
    ) -> bool:
        """Register this host as available for distributed work."""
        try:
            data = {
                "host_id": self.host_id,
                "host_url": self.host_url,
                "capabilities": capabilities,
                "compute_budget": compute_budget or {},
                "agent_ids": agent_ids or [],
                "registered_at": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            }
            self._redis.hset(self.HOSTS_HASH, self.host_id, json.dumps(data))
            logger.info(f"Host registered: {self.host_id} with {len(capabilities)} capabilities")
            return True
        except Exception as e:
            logger.error(f"Failed to register host {self.host_id}: {e}")
            return False

    def deregister_host(self) -> bool:
        """Remove this host from the registry."""
        try:
            self._redis.hdel(self.HOSTS_HASH, self.host_id)
            logger.info(f"Host deregistered: {self.host_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to deregister host: {e}")
            return False

    # ── stale-host purging ────────────────────────────────────────
    def _purge_stale(self) -> int:
        """Remove host entries whose ``last_seen`` is older than STALE_THRESHOLD.

        Returns the number of purged entries.
        """
        purged = 0
        try:
            now = datetime.now()
            all_data = self._redis.hgetall(self.HOSTS_HASH)
            for hid, raw in all_data.items():
                try:
                    entry = json.loads(raw)
                    last_seen_str = entry.get("last_seen")
                    if not last_seen_str:
                        # No timestamp — treat as stale
                        self._redis.hdel(self.HOSTS_HASH, hid)
                        purged += 1
                        continue
                    last_seen = datetime.fromisoformat(last_seen_str)
                    if (now - last_seen).total_seconds() > STALE_THRESHOLD:
                        self._redis.hdel(self.HOSTS_HASH, hid)
                        purged += 1
                        logger.info(f"Purged stale host: {hid} (last seen {last_seen_str})")
                except (json.JSONDecodeError, ValueError):
                    # Corrupt entry — remove it
                    self._redis.hdel(self.HOSTS_HASH, hid)
                    purged += 1
        except Exception as e:
            logger.error(f"Failed to purge stale hosts: {e}")
        return purged

    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """List all registered hosts (purges stale entries first)."""
        self._purge_stale()
        hosts = []
        try:
            all_data = self._redis.hgetall(self.HOSTS_HASH)
            for host_id, raw in all_data.items():
                hosts.append(json.loads(raw))
        except Exception as e:
            logger.error(f"Failed to list hosts: {e}")
        return hosts

    def get_hosts_with_capability(self, capability: str) -> List[Dict[str, Any]]:
        """Find hosts that have a specific capability."""
        return [
            h for h in self.get_all_hosts()
            if capability in h.get("capabilities", [])
        ]

    def update_compute_usage(self, usage: Dict[str, Any]) -> None:
        """Report current compute usage (CPU, memory, active tasks)."""
        try:
            raw = self._redis.hget(self.HOSTS_HASH, self.host_id)
            if raw:
                data = json.loads(raw)
                data["compute_usage"] = usage
                data["last_seen"] = datetime.now().isoformat()
                self._redis.hset(self.HOSTS_HASH, self.host_id, json.dumps(data))
        except Exception as e:
            logger.debug(f"Failed to update compute usage: {e}")

    def get_host_info(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Get info for a specific host."""
        try:
            raw = self._redis.hget(self.HOSTS_HASH, host_id)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None
