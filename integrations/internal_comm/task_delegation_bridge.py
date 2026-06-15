"""
Task Delegation Bridge - Integrates A2A delegation with SmartLedger

This module bridges the gap between A2A delegation and task_ledger,
ensuring that delegated tasks are properly tracked with state management
and auto-resume capabilities.

Key Features:
- Parent task goes BLOCKED while delegation is in progress
- Delegated task created in ledger with proper parent-child relationship
- Automatic resume of parent task when delegation completes
- Full audit trail of delegation lifecycle
- Nested task support for complex delegations
"""

import logging
import json
from typing import Optional, Dict, Any, List
from datetime import datetime
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from agent_ledger import (
    SmartLedger, Task, TaskType, TaskStatus, ExecutionMode
)
from integrations.internal_comm.internal_agent_communication import (
    A2AContextExchange, skill_registry
)

logger = logging.getLogger(__name__)


class TaskDelegationBridge:
    """
    Bridges A2A delegation with SmartLedger for proper state management

    Workflow:
    1. Agent A has task T1 in progress
    2. Agent A delegates subtask T2 to Agent B
    3. T1 → BLOCKED (waiting for delegation)
    4. T2 → Created as child of T1, assigned to Agent B
    5. Agent B processes T2
    6. When T2 → COMPLETED, T1 → Auto-resumes to IN_PROGRESS
    """

    def __init__(self, a2a_context: A2AContextExchange, ledger: SmartLedger):
        """
        Initialize delegation bridge

        Args:
            a2a_context: A2A context exchange for delegation
            ledger: SmartLedger for task tracking
        """
        self.a2a_context = a2a_context
        self.ledger = ledger
        self.delegation_map = {}  # delegation_id -> (parent_task_id, child_task_id)

    def delegate_task_with_tracking(
        self,
        parent_task_id: str,
        from_agent: str,
        task_description: str,
        required_skills: List[str],
        context: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Delegate a task with full task_ledger integration

        Args:
            parent_task_id: ID of the parent task (will be BLOCKED)
            from_agent: Agent delegating the task
            task_description: Description of task to delegate
            required_skills: Required skills for the task
            context: Optional context data

        Returns:
            Delegation ID or None if delegation fails
        """
        # Step 1: Get parent task
        parent_task = self.ledger.get_task(parent_task_id)
        if not parent_task:
            logger.error(f"Parent task not found: {parent_task_id}")
            return None

        # Step 2: Delegate via A2A
        delegation_id = self.a2a_context.delegate_task(
            from_agent=from_agent,
            task=task_description,
            required_skills=required_skills,
            context=context
        )

        if not delegation_id:
            logger.error("A2A delegation failed - no suitable agent found")
            return None

        # Step 3: Get delegation info to find target agent
        delegation_info = self.a2a_context.delegations.get(delegation_id)
        if not delegation_info:
            logger.error(f"Delegation info not found: {delegation_id}")
            return None

        to_agent = delegation_info['to_agent']

        # Step 4: Create child task in ledger with parent-child relationship
        child_task = self.ledger.create_parent_child_task(
            parent_task_id=parent_task_id,
            child_description=task_description,
            child_type=TaskType.AUTONOMOUS,
            context={
                'delegation_id': delegation_id,
                'delegated_by': from_agent,
                'delegated_to': to_agent,
                'required_skills': required_skills,
                'delegation_context': context or {}
            }
        )

        if not child_task:
            logger.error(f"Failed to create child task for delegation {delegation_id}")
            return None

        # Step 6: Block parent task (waiting for delegation)
        self.ledger.update_task_status(
            parent_task_id,
            TaskStatus.BLOCKED,
            f"Waiting for delegated task: {child_task.task_id}"
        )

        # Step 7: Map delegation to task IDs
        self.delegation_map[delegation_id] = {
            'parent_task_id': parent_task_id,
            'child_task_id': child_task.task_id,
            'from_agent': from_agent,
            'to_agent': to_agent,
            'created_at': datetime.now().isoformat()
        }

        logger.info(
            f"Task delegation tracked: {delegation_id}\n"
            f"  Parent task {parent_task_id} → BLOCKED\n"
            f"  Child task {child_task.task_id} → Created for {to_agent}"
        )

        return delegation_id

    def complete_delegation_with_tracking(
        self,
        delegation_id: str,
        result: Any,
        success: bool = True
    ) -> bool:
        """
        Complete a delegation and update task states

        Args:
            delegation_id: Delegation ID
            result: Delegation result
            success: Whether delegation succeeded

        Returns:
            True if completion successful
        """
        # Step 1: Get delegation mapping
        if delegation_id not in self.delegation_map:
            logger.error(f"Delegation mapping not found: {delegation_id}")
            return False

        mapping = self.delegation_map[delegation_id]
        parent_task_id = mapping['parent_task_id']
        child_task_id = mapping['child_task_id']

        # Step 2: Complete delegation in A2A
        self.a2a_context.complete_delegation(delegation_id, result)

        # Step 3: Update child task status
        child_status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
        self.ledger.update_task_status(
            child_task_id,
            child_status,
            json.dumps(result) if isinstance(result, (dict, list)) else str(result)
        )

        # Step 4: Auto-resume parent task (task_ledger should handle this automatically)
        # But let's explicitly trigger it to be safe
        parent_task = self.ledger.get_task(parent_task_id)
        if parent_task and parent_task.status == TaskStatus.BLOCKED:
            # Check if all dependencies are complete
            if self._all_dependencies_complete(parent_task_id):
                self.ledger.update_task_status(
                    parent_task_id,
                    TaskStatus.IN_PROGRESS,
                    f"Resumed after delegation {delegation_id} completed"
                )
                logger.info(f"Parent task {parent_task_id} auto-resumed after delegation")

        logger.info(
            f"Delegation completed: {delegation_id}\n"
            f"  Child task {child_task_id} → {child_status.value}\n"
            f"  Parent task {parent_task_id} → Resumed"
        )

        return True

    def _all_dependencies_complete(self, task_id: str) -> bool:
        """Check if all dependencies of a task are complete"""
        task = self.ledger.get_task(task_id)
        if not task or not task.depends_on:
            return True

        for dep_id in task.depends_on:
            dep_task = self.ledger.get_task(dep_id)
            if not dep_task or not TaskStatus.is_terminal_state(dep_task.status):
                return False

        return True

    def get_delegation_status(self, delegation_id: str) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive status of a delegation

        Args:
            delegation_id: Delegation ID

        Returns:
            Dictionary with delegation status including task states
        """
        if delegation_id not in self.delegation_map:
            return None

        mapping = self.delegation_map[delegation_id]
        parent_task = self.ledger.get_task(mapping['parent_task_id'])
        child_task = self.ledger.get_task(mapping['child_task_id'])
        a2a_delegation = self.a2a_context.delegations.get(delegation_id)

        return {
            'delegation_id': delegation_id,
            'parent_task': {
                'task_id': parent_task.task_id if parent_task else None,
                'status': parent_task.status.value if parent_task else None,
                'description': parent_task.description if parent_task else None
            },
            'child_task': {
                'task_id': child_task.task_id if child_task else None,
                'status': child_task.status.value if child_task else None,
                'description': child_task.description if child_task else None
            },
            'delegation': a2a_delegation,
            'mapping': mapping
        }

    def list_active_delegations(self) -> List[Dict[str, Any]]:
        """Get all active delegations with their task states"""
        active = []

        for delegation_id, mapping in self.delegation_map.items():
            child_task = self.ledger.get_task(mapping['child_task_id'])

            # Only include if child task is not in terminal state
            if child_task and not TaskStatus.is_terminal_state(child_task.status):
                status = self.get_delegation_status(delegation_id)
                if status:
                    active.append(status)

        return active


def create_delegation_function_with_ledger(
    agent_name: str,
    ledger: SmartLedger,
    a2a_context: A2AContextExchange,
    current_task_id: Optional[str] = None
):
    """
    Create a delegation function that integrates with task_ledger

    Args:
        agent_name: Name of the agent
        ledger: SmartLedger instance
        a2a_context: A2A context exchange
        current_task_id: Current task ID (will be blocked during delegation)

    Returns:
        Delegation function for autogen
    """
    bridge = TaskDelegationBridge(a2a_context, ledger)

    def delegate_with_tracking(
        task: str,
        required_skills: List[str],
        context: Optional[Dict] = None
    ) -> str:
        """
        Delegate a task to a specialist agent with full tracking

        Args:
            task: Task description
            required_skills: Required skills
            context: Optional context

        Returns:
            JSON result with delegation status
        """
        # If we have a current task, use it as parent
        parent_task_id = current_task_id

        # If no current task, create one
        if not parent_task_id:
            parent_task_id = f"task_delegation_{uuid.uuid4().hex[:12]}"
            parent_task = Task(
                task_id=parent_task_id,
                description=f"{agent_name} - delegating task",
                task_type=TaskType.AUTONOMOUS,
                context={'delegating_agent': agent_name}
            )
            ledger.add_task(parent_task)

        # Delegate with tracking
        delegation_id = bridge.delegate_task_with_tracking(
            parent_task_id=parent_task_id,
            from_agent=agent_name,
            task_description=task,
            required_skills=required_skills,
            context=context
        )

        if delegation_id:
            status = bridge.get_delegation_status(delegation_id)
            return json.dumps({
                'success': True,
                'delegation_id': delegation_id,
                'message': f'Task delegated to specialist agent',
                'status': status
            }, indent=2)
        else:
            return json.dumps({
                'success': False,
                'error': 'No suitable agent found for delegation'
            }, indent=2)

    return delegate_with_tracking


# Convenience exports
__all__ = [
    'TaskDelegationBridge',
    'create_delegation_function_with_ledger'
]
