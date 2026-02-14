"""
Distributed Task Coordinator — cross-host task orchestration for ANY agent type.

Composes existing components:
- SmartLedger (agent_ledger.core) for task state management
- DistributedTaskLock (agent_ledger.distributed) for atomic task claiming
- LedgerPubSub (agent_ledger.pubsub) for cross-host notifications
- TaskVerification + TaskBaseline (agent_ledger.verification) for trust
- RegionalHostRegistry (this package) for host discovery

Workflow:
1. Any host decomposes a goal into tasks (coding, research, music, etc.)
2. Tasks are published to shared Redis
3. Regional hosts claim tasks atomically (DistributedTaskLock)
4. Results are verified via SHA-256 hashing
5. Progress is baselined periodically
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent_ledger.core import SmartLedger, Task, TaskType, TaskStatus
from agent_ledger.distributed import DistributedTaskLock
from agent_ledger.verification import TaskVerification, TaskBaseline

logger = logging.getLogger(__name__)


class DistributedTaskCoordinator:
    """
    Coordinates task delegation across regional hosts.

    Agent-type agnostic — works for coding agents, research agents,
    music agents, or any other domain. The `context` dict on each task
    carries domain-specific metadata (repo_url, genre, search_query, etc.).
    """

    def __init__(
        self,
        ledger: SmartLedger,
        task_lock: DistributedTaskLock,
        verifier: Optional[TaskVerification] = None,
        baseline: Optional[TaskBaseline] = None,
    ):
        self._ledger = ledger
        self._lock = task_lock
        self._verifier = verifier or TaskVerification()
        self._baseline = baseline or TaskBaseline(ledger.backend)

    def submit_goal(
        self,
        objective: str,
        decomposed_tasks: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Submit a goal with pre-decomposed tasks.

        Args:
            objective: High-level goal description (any domain)
            decomposed_tasks: List of {"task_id": ..., "description": ..., "capabilities": [...]}
            context: Optional domain-specific metadata (e.g. repo_url, genre, dataset_path).
                     Stored on the parent task and inherited by children.

        Returns:
            goal_id (parent task ID)
        """
        goal_id = f"goal_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        context = context or {}

        # Create parent goal task
        parent = Task(
            task_id=goal_id,
            description=f"[GOAL] {objective}",
            task_type=TaskType.PRE_ASSIGNED,
        )
        parent.context["objective"] = objective
        parent.context.update(context)
        self._ledger.add_task(parent)
        self._ledger.update_task_status(goal_id, TaskStatus.IN_PROGRESS)

        # Create child tasks
        for task_def in decomposed_tasks:
            child_id = task_def.get("task_id", f"{goal_id}_sub_{len(self._ledger.tasks)}")
            child = Task(
                task_id=child_id,
                description=task_def["description"],
                task_type=TaskType.AUTONOMOUS,
            )
            child.context["capabilities_required"] = task_def.get("capabilities", [])
            # Inherit parent context so children know the domain
            child.context.update(context)
            child.parent_task_id = goal_id
            self._ledger.add_task(child)
            parent.child_task_ids.append(child_id)

        self._ledger.save()

        # Create initial baseline
        self._baseline.create_snapshot(self._ledger, label=f"goal_submitted_{goal_id}")

        logger.info(f"Goal submitted: {goal_id} with {len(decomposed_tasks)} tasks")
        return goal_id

    def claim_next_task(
        self,
        agent_id: str,
        capabilities: Optional[List[str]] = None,
    ) -> Optional[Task]:
        """
        Atomically claim the next available task matching capabilities.

        Uses DistributedTaskLock to prevent double-claiming.
        """
        for task_id in self._ledger.task_order:
            task = self._ledger.get_task(task_id)
            if not task or task.status != TaskStatus.PENDING:
                continue

            # Check capability match
            if capabilities:
                required = task.context.get("capabilities_required", [])
                if required and not any(c in capabilities for c in required):
                    continue

            # Try atomic claim
            if self._lock.try_claim_task(task_id, agent_id):
                self._ledger.update_task_status(task_id, TaskStatus.IN_PROGRESS)
                task.context["claimed_by"] = agent_id
                task.context["claimed_at"] = datetime.now().isoformat()
                self._ledger.save()
                logger.info(f"Task {task_id} claimed by {agent_id}")
                return task

        logger.debug(f"No available tasks for agent {agent_id}")
        return None

    def submit_result(
        self,
        task_id: str,
        agent_id: str,
        result: Any,
    ) -> Dict[str, Any]:
        """
        Submit a task result for verification.

        Computes SHA-256 hash and publishes verification request via PubSub.
        """
        result_hash = TaskVerification.compute_result_hash(result)

        self._ledger.complete_task(task_id, result=result)
        self._lock.release_task(task_id, agent_id)

        # Store result_hash so verify_result() can compare later
        task = self._ledger.get_task(task_id)
        if task:
            task.context["result_hash"] = result_hash
            self._ledger.save()

        # Publish verification request if pubsub is enabled
        if hasattr(self._ledger, '_pubsub') and self._ledger._pubsub:
            self._ledger._pubsub.publish_verification_request(task_id, result_hash)
        self._notify_goal_contribution(
            task_id, agent_id,
            task.description if task else "a task",
        )

        logger.info(f"Result submitted: task={task_id} agent={agent_id} hash={result_hash[:16]}...")
        return {
            "task_id": task_id,
            "result_hash": result_hash,
            "status": "completed",
        }

    def _notify_goal_contribution(self, task_id: str, agent_id: str, task_description: str):
        """Notify the user who owns the agent that their agent contributed to a goal."""
        try:
            task = self._ledger.get_task(task_id)
            if not task or not task.parent_task_id:
                return
            parent = self._ledger.get_task(task.parent_task_id)
            if not parent:
                return

            objective = parent.context.get("objective", parent.description)
            user_id = agent_id  # agent_id IS str(g.user.id) — set in api.py

            # Use Flask request context db if available, else open a fresh session
            try:
                from flask import g as flask_g
                db = flask_g.db
                owns_session = False
            except (RuntimeError, AttributeError):
                from integrations.social.models import get_db
                db = get_db()
                owns_session = True

            from integrations.social.services import NotificationService
            message = f'Your agent contributed to "{objective}": completed "{task_description}"'
            notif = NotificationService.create(
                db, user_id, 'goal_contribution',
                source_user_id=None,
                target_type='goal',
                target_id=task.parent_task_id,
                message=message,
            )

            if owns_session:
                db.commit()
                db.close()

            # Push real-time notification via WAMP (fires silently if Crossbar unavailable)
            try:
                from integrations.social.realtime import on_notification
                on_notification(user_id, notif.to_dict())
            except Exception:
                pass

            logger.info(f"Notified user {user_id}: goal contribution for {task_id}")
        except Exception as e:
            logger.warning(f"Failed to notify goal contribution: {e}")

    def verify_result(self, task_id: str, verifying_agent: str) -> bool:
        """
        Verify another agent's task result.

        Re-computes hash and compares. Records verification.
        """
        task = self._ledger.get_task(task_id)
        if not task or task.result is None:
            logger.warning(f"Cannot verify task {task_id}: no result")
            return False

        current_hash = TaskVerification.compute_result_hash(task.result)
        stored_hash = task.context.get("result_hash", "")

        verified = current_hash == stored_hash
        self._verifier.record_verification(task_id, current_hash, verifying_agent, verified)

        logger.info(f"Verification: task={task_id} by={verifying_agent} passed={verified}")
        return verified

    def get_goal_progress(self, goal_id: str) -> Dict[str, Any]:
        """Get progress across all hosts for a goal."""
        parent = self._ledger.get_task(goal_id)
        if not parent:
            return {"error": f"Goal {goal_id} not found"}

        child_ids = parent.child_task_ids
        children = []
        completed = 0

        for child_id in child_ids:
            child = self._ledger.get_task(child_id)
            if child:
                status = child.status.value if hasattr(child.status, "value") else str(child.status)
                children.append({
                    "task_id": child_id,
                    "description": child.description,
                    "status": status,
                    "claimed_by": child.context.get("claimed_by"),
                    "result_hash": child.context.get("result_hash"),
                })
                if child.status == TaskStatus.COMPLETED:
                    completed += 1

        total = len(child_ids) or 1
        return {
            "goal_id": goal_id,
            "objective": parent.context.get("objective", parent.description),
            "context": {k: v for k, v in parent.context.items() if k != "objective"},
            "total_tasks": len(child_ids),
            "completed": completed,
            "progress_pct": round(completed / total * 100, 1),
            "tasks": children,
        }

    def create_baseline(self, label: str = "") -> str:
        """Create a snapshot baseline of current progress."""
        return self._baseline.create_snapshot(self._ledger, label)

    def compare_to_baseline(self, snapshot_id: str) -> Dict[str, Any]:
        """Compare current state against a baseline."""
        return self._baseline.compare_to_snapshot(self._ledger, snapshot_id)
