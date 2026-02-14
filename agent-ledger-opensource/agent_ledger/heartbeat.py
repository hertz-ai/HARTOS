"""
Agent Heartbeat — liveness tracking for distributed agents via Redis.

Each agent periodically sets a Redis key with TTL. Other agents detect
stale/dead agents by checking if the key has expired, then reclaim
their delegated tasks.

Usage:
    from agent_ledger.heartbeat import AgentHeartbeat

    hb = AgentHeartbeat(redis_client, "agent_A", host_info={"region": "us-east"})
    hb.start()   # daemon thread — auto-stops on process exit

    hb.is_agent_alive("agent_B")      # True/False
    hb.get_alive_agents()             # [{agent_id, host_info, last_seen}]
    hb.get_stale_agents()             # ["agent_C"]  (heartbeat expired)

    hb.stop()
"""

import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentHeartbeat:
    """Periodic liveness tracking for distributed agents via Redis SETEX."""

    KEY_PREFIX = "agent_ledger:heartbeat:"
    HEARTBEAT_INTERVAL = 30   # seconds between heartbeats
    STALE_THRESHOLD = 90      # seconds before considered dead (3 missed)

    def __init__(self, redis_client, agent_id: str, host_info: Optional[Dict[str, Any]] = None):
        self._redis = redis_client
        self._agent_id = agent_id
        self._host_info = host_info or {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start periodic heartbeat publishing in a daemon thread."""
        if self._running:
            return
        self._running = True
        self._beat()  # immediate first beat
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"heartbeat-{self._agent_id}")
        self._thread.start()
        logger.info(f"Heartbeat started for {self._agent_id} (interval={self.HEARTBEAT_INTERVAL}s)")

    def stop(self) -> None:
        """Stop heartbeat publishing."""
        self._running = False
        # Remove heartbeat key
        try:
            self._redis.delete(f"{self.KEY_PREFIX}{self._agent_id}")
        except Exception:
            pass
        logger.info(f"Heartbeat stopped for {self._agent_id}")

    def _loop(self) -> None:
        """Background loop — publishes heartbeat every HEARTBEAT_INTERVAL seconds."""
        import time
        while self._running:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if self._running:
                self._beat()

    def _beat(self) -> None:
        """Publish a single heartbeat."""
        try:
            key = f"{self.KEY_PREFIX}{self._agent_id}"
            data = json.dumps({
                "agent_id": self._agent_id,
                "host_info": self._host_info,
                "last_seen": datetime.now().isoformat(),
            })
            self._redis.setex(key, self.STALE_THRESHOLD, data)
        except Exception as e:
            logger.debug(f"Heartbeat publish failed: {e}")

    def is_agent_alive(self, agent_id: str) -> bool:
        """Check if a specific agent has an active heartbeat."""
        try:
            return self._redis.exists(f"{self.KEY_PREFIX}{agent_id}") > 0
        except Exception:
            return False

    def get_alive_agents(self) -> List[Dict[str, Any]]:
        """List all agents with active heartbeats."""
        agents = []
        try:
            keys = list(self._redis.scan_iter(match=f"{self.KEY_PREFIX}*", count=100))
            for key in keys:
                data = self._redis.get(key)
                if data:
                    agents.append(json.loads(data))
        except Exception as e:
            logger.debug(f"Failed to list alive agents: {e}")
        return agents

    def get_stale_agents(self, known_agent_ids: Optional[List[str]] = None) -> List[str]:
        """
        Find agents whose heartbeat has expired.

        Args:
            known_agent_ids: List of agent IDs to check. If None, returns empty
                            (can't detect stale without knowing who should be alive).
        """
        if not known_agent_ids:
            return []
        stale = []
        for agent_id in known_agent_ids:
            if not self.is_agent_alive(agent_id):
                stale.append(agent_id)
        return stale

    def get_agent_host_info(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get host information for a specific agent."""
        try:
            data = self._redis.get(f"{self.KEY_PREFIX}{agent_id}")
            if data:
                return json.loads(data).get("host_info")
        except Exception:
            pass
        return None
