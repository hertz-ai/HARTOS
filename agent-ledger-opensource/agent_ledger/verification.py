"""
Task Verification — SHA-256 result hashing + baselining for distributed trust.

Pure Python, zero dependencies. Uses Redis only when available.

Usage:
    from agent_ledger.verification import TaskVerification, TaskBaseline

    # Hash a result
    h = TaskVerification.compute_result_hash({"code": "print('hello')"})

    # Verify another agent's result
    verifier = TaskVerification()
    verifier.record_verification("task_1", h, "agent_B", verified=True)

    # Baseline entire ledger state
    baseline = TaskBaseline(ledger.backend)
    snap_id = baseline.create_snapshot(ledger, label="v1")
    diff = baseline.compare_to_snapshot(ledger, snap_id)
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskVerification:
    """SHA-256 based verification of task results for distributed trust."""

    def __init__(self, redis_client=None):
        """
        Initialize. Redis is optional — verifications stored in-memory if no Redis.

        Args:
            redis_client: Optional redis.Redis instance for distributed storage
        """
        self._redis = redis_client
        self._local_store: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def compute_result_hash(result: Any) -> str:
        """Compute SHA-256 hash of a task result."""
        serialized = json.dumps(result, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def record_verification(
        self,
        task_id: str,
        result_hash: str,
        verifying_agent: str,
        verified: bool,
    ) -> None:
        """Record that an agent verified (or rejected) a task result."""
        record = {
            "agent_id": verifying_agent,
            "result_hash": result_hash,
            "verified": verified,
            "timestamp": datetime.now().isoformat(),
        }

        if self._redis:
            key = f"agent_ledger:verification:{task_id}"
            self._redis.rpush(key, json.dumps(record))
        else:
            if task_id not in self._local_store:
                self._local_store[task_id] = {"verifications": []}
            self._local_store[task_id]["verifications"].append(record)

        logger.info(f"Verification recorded: task={task_id} agent={verifying_agent} verified={verified}")

    def get_verification_status(self, task_id: str) -> Dict[str, Any]:
        """Get all verifications for a task."""
        verifications = []

        if self._redis:
            key = f"agent_ledger:verification:{task_id}"
            raw = self._redis.lrange(key, 0, -1)
            verifications = [json.loads(r) for r in raw]
        else:
            entry = self._local_store.get(task_id, {})
            verifications = entry.get("verifications", [])

        if not verifications:
            return {"task_id": task_id, "verifications": [], "consensus": None}

        agreed = sum(1 for v in verifications if v["verified"])
        total = len(verifications)
        consensus = agreed > total / 2 if total > 0 else None

        return {
            "task_id": task_id,
            "verifications": verifications,
            "agreed": agreed,
            "total": total,
            "consensus": consensus,
        }

    def requires_verification(self, task_id: str, min_verifiers: int = 2) -> bool:
        """Check if a task still needs more verifications."""
        status = self.get_verification_status(task_id)
        return status["total"] < min_verifiers


class TaskBaseline:
    """Snapshot-based baselining for tracking progress against previous states."""

    def __init__(self, backend=None):
        """
        Initialize with storage backend.

        Args:
            backend: StorageBackend instance (JSONBackend, RedisBackend, etc.)
                     If None, snapshots stored in-memory only.
        """
        self._backend = backend
        self._local_snapshots: Dict[str, Dict[str, Any]] = {}

    def create_snapshot(self, ledger, label: str = "") -> str:
        """
        Create a point-in-time snapshot of the entire ledger state.

        Args:
            ledger: SmartLedger instance
            label: Optional human-readable label

        Returns:
            snapshot_id
        """
        snapshot_id = f"snap_{ledger.agent_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        snapshot = {
            "snapshot_id": snapshot_id,
            "label": label,
            "agent_id": ledger.agent_id,
            "session_id": ledger.session_id,
            "created_at": datetime.now().isoformat(),
            "task_count": len(ledger.tasks),
            "tasks": {},
        }

        for task_id, task in ledger.tasks.items():
            snapshot["tasks"][task_id] = {
                "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "description": task.description,
                "result_hash": task.context.get("result_hash") if hasattr(task, "context") else None,
            }

        if self._backend:
            self._backend.save(f"snapshot_{snapshot_id}", snapshot)
        else:
            self._local_snapshots[snapshot_id] = snapshot

        logger.info(f"Created snapshot: {snapshot_id} ({len(snapshot['tasks'])} tasks, label='{label}')")
        return snapshot_id

    def compare_to_snapshot(self, ledger, snapshot_id: str) -> Dict[str, Any]:
        """
        Compare current ledger state against a previous snapshot.

        Returns:
            {new_tasks, completed_since, changed_tasks, removed_tasks}
        """
        if self._backend:
            snapshot = self._backend.load(f"snapshot_{snapshot_id}")
        else:
            snapshot = self._local_snapshots.get(snapshot_id)

        if not snapshot:
            return {"error": f"Snapshot {snapshot_id} not found"}

        snap_tasks = snapshot.get("tasks", {})
        current_ids = set(ledger.tasks.keys())
        snap_ids = set(snap_tasks.keys())

        new_tasks = list(current_ids - snap_ids)
        removed_tasks = list(snap_ids - current_ids)

        completed_since = []
        changed_tasks = []

        for task_id in current_ids & snap_ids:
            current_status = ledger.tasks[task_id].status
            current_val = current_status.value if hasattr(current_status, "value") else str(current_status)
            snap_status = snap_tasks[task_id]["status"]

            if current_val != snap_status:
                changed_tasks.append({
                    "task_id": task_id,
                    "was": snap_status,
                    "now": current_val,
                })
                if current_val == "completed":
                    completed_since.append(task_id)

        return {
            "snapshot_id": snapshot_id,
            "snapshot_label": snapshot.get("label", ""),
            "new_tasks": new_tasks,
            "completed_since": completed_since,
            "changed_tasks": changed_tasks,
            "removed_tasks": removed_tasks,
        }

    def get_snapshots(self, agent_id: str = None) -> List[Dict[str, Any]]:
        """List all snapshots, optionally filtered by agent_id."""
        snapshots = []

        if self._backend:
            keys = self._backend.list_keys("snapshot_snap_*")
            for key in keys:
                data = self._backend.load(key)
                if data and (agent_id is None or data.get("agent_id") == agent_id):
                    snapshots.append({
                        "snapshot_id": data["snapshot_id"],
                        "label": data.get("label", ""),
                        "agent_id": data.get("agent_id"),
                        "created_at": data.get("created_at"),
                        "task_count": data.get("task_count", 0),
                    })
        else:
            for snap in self._local_snapshots.values():
                if agent_id is None or snap.get("agent_id") == agent_id:
                    snapshots.append({
                        "snapshot_id": snap["snapshot_id"],
                        "label": snap.get("label", ""),
                        "agent_id": snap.get("agent_id"),
                        "created_at": snap.get("created_at"),
                        "task_count": snap.get("task_count", 0),
                    })

        return snapshots
