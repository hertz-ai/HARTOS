"""
Verification Protocol — multi-agent consensus for distributed task results.

Steps:
1. SUBMIT: Agent completes task, computes result hash
2. VERIFY: Other agents independently verify (hash match + optional LLM review)
3. CONSENSUS: If majority agree (>50%), result accepted. Else → task back to PENDING.
4. BASELINE: After acceptance, new snapshot created.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from agent_ledger.core import SmartLedger, TaskStatus
from agent_ledger.verification import TaskVerification, TaskBaseline

logger = logging.getLogger(__name__)


class VerificationProtocol:
    """Multi-step verification protocol for distributed task results."""

    MIN_VERIFIERS = 2
    CONSENSUS_THRESHOLD = 0.5  # >50% must agree

    def __init__(
        self,
        ledger: SmartLedger,
        verifier: Optional[TaskVerification] = None,
        baseline: Optional[TaskBaseline] = None,
    ):
        self._ledger = ledger
        self._verifier = verifier or TaskVerification()
        self._baseline = baseline or TaskBaseline(ledger.backend)

    def request_verification(self, task_id: str) -> Dict[str, Any]:
        """
        Create a verification request for a completed task.

        Returns verification request info including result hash.
        """
        task = self._ledger.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        if task.status != TaskStatus.COMPLETED:
            return {"error": f"Task {task_id} not completed (status={task.status})"}

        result_hash = task.context.get("result_hash")
        if not result_hash and task.result is not None:
            result_hash = TaskVerification.compute_result_hash(task.result)
            task.context["result_hash"] = result_hash
            self._ledger.save()

        request = {
            "task_id": task_id,
            "result_hash": result_hash,
            "requested_at": datetime.now().isoformat(),
            "min_verifiers": self.MIN_VERIFIERS,
        }

        # Publish via PubSub if available
        if hasattr(self._ledger, '_pubsub') and self._ledger._pubsub:
            self._ledger._pubsub.publish_verification_request(task_id, result_hash or "")

        logger.info(f"Verification requested for task {task_id}")
        return request

    def submit_verification(
        self,
        task_id: str,
        verifying_agent: str,
        verdict: bool,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Submit a verification verdict."""
        task = self._ledger.get_task(task_id)
        if not task:
            logger.warning(f"Cannot verify task {task_id}: not found")
            return

        result_hash = task.context.get("result_hash", "")
        self._verifier.record_verification(task_id, result_hash, verifying_agent, verdict)

        if evidence:
            # Store evidence in task context
            if "verification_evidence" not in task.context:
                task.context["verification_evidence"] = []
            task.context["verification_evidence"].append({
                "agent": verifying_agent,
                "verdict": verdict,
                "evidence": evidence,
                "timestamp": datetime.now().isoformat(),
            })
            self._ledger.save()

        logger.info(f"Verification submitted: task={task_id} agent={verifying_agent} verdict={verdict}")

    def check_consensus(self, task_id: str) -> Dict[str, Any]:
        """
        Check if consensus has been reached.

        Returns: {consensus_reached, accepted, verifications, agreed, total}
        """
        status = self._verifier.get_verification_status(task_id)

        total = status.get("total", 0)
        agreed = status.get("agreed", 0)

        enough_verifiers = total >= self.MIN_VERIFIERS
        accepted = agreed > total * self.CONSENSUS_THRESHOLD if total > 0 else False
        consensus_reached = enough_verifiers

        result = {
            "task_id": task_id,
            "consensus_reached": consensus_reached,
            "accepted": accepted if consensus_reached else None,
            "agreed": agreed,
            "total": total,
            "min_verifiers": self.MIN_VERIFIERS,
            "threshold": self.CONSENSUS_THRESHOLD,
        }

        # Auto-handle rejection if consensus reached and rejected
        if consensus_reached and not accepted:
            self.handle_rejection(task_id, "Consensus rejected the result")
            result["action_taken"] = "task_reset_to_pending"

        # Auto-baseline if accepted
        if consensus_reached and accepted:
            snap_id = self._baseline.create_snapshot(self._ledger, label=f"verified_{task_id}")
            result["baseline_snapshot"] = snap_id
            self._notify_verification_accepted(task_id)

        return result

    def _notify_verification_accepted(self, task_id: str):
        """Notify the agent owner that their contribution was verified by consensus."""
        try:
            task = self._ledger.get_task(task_id)
            if not task:
                return
            agent_id = task.context.get("claimed_by")
            if not agent_id:
                return

            parent = self._ledger.get_task(task.parent_task_id) if task.parent_task_id else None
            objective = parent.context.get("objective", "a distributed goal") if parent else "a distributed goal"

            try:
                from flask import g as flask_g
                db = flask_g.db
                owns_session = False
            except (RuntimeError, AttributeError):
                from integrations.social.models import get_db
                db = get_db()
                owns_session = True

            from integrations.social.services import NotificationService
            message = f'Your contribution to "{objective}" was verified by peer consensus!'
            notif = NotificationService.create(
                db, agent_id, 'goal_verified',
                source_user_id=None,
                target_type='goal',
                target_id=task.parent_task_id or task_id,
                message=message,
            )

            if owns_session:
                db.commit()
                db.close()

            try:
                from integrations.social.realtime import on_notification
                on_notification(agent_id, notif.to_dict())
            except Exception:
                pass

            logger.info(f"Notified user {agent_id}: verification accepted for {task_id}")
        except Exception as e:
            logger.warning(f"Failed to notify verification consensus: {e}")

    def handle_rejection(self, task_id: str, reason: str) -> None:
        """Handle a rejected result — rollback task to PENDING."""
        task = self._ledger.get_task(task_id)
        if not task:
            return

        # Rollback completed task, then retry from PENDING if rollback succeeds
        if task.status == TaskStatus.COMPLETED:
            task.rollback(reason=reason)
        # For non-terminal states, fail the task so it can be retried
        if not task.is_terminal():
            task.fail(error=reason, reason="Verification rejected")

        logger.info(f"Task {task_id} rejected and reset to PENDING: {reason}")
