"""
Workflow Engine for HevolveBot Integration.

Provides workflow definition and execution capabilities.
"""

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union
import copy


class StepType(Enum):
    """Types of workflow steps."""
    ACTION = "action"
    CONDITION = "condition"
    LOOP = "loop"
    PARALLEL = "parallel"
    DELAY = "delay"
    SUBPROCESS = "subprocess"
    TRANSFORM = "transform"


class WorkflowStatus(Enum):
    """Status of a workflow execution."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkflowStep:
    """A single step in a workflow."""
    id: str
    name: str
    step_type: StepType
    action: Optional[Callable] = None
    condition: Optional[Callable[[Dict[str, Any]], bool]] = None
    transform: Optional[Callable[[Any], Any]] = None
    on_true: Optional[str] = None  # Next step if condition is true
    on_false: Optional[str] = None  # Next step if condition is false
    next_step: Optional[str] = None  # Default next step
    delay_seconds: float = 0
    retry_count: int = 0
    max_retries: int = 3
    parallel_steps: List[str] = field(default_factory=list)
    subprocess_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    """A complete workflow definition."""
    id: str
    name: str
    description: str = ""
    steps: Dict[str, WorkflowStep] = field(default_factory=dict)
    entry_point: Optional[str] = None
    version: str = "1.0.0"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_step(self, step: WorkflowStep) -> None:
        """Add a step to the workflow."""
        self.steps[step.id] = step
        if self.entry_point is None:
            self.entry_point = step.id
        self.updated_at = datetime.now()

    def remove_step(self, step_id: str) -> bool:
        """Remove a step from the workflow."""
        if step_id in self.steps:
            del self.steps[step_id]
            if self.entry_point == step_id:
                self.entry_point = next(iter(self.steps), None)
            self.updated_at = datetime.now()
            return True
        return False

    def get_step(self, step_id: str) -> Optional[WorkflowStep]:
        """Get a step by ID."""
        return self.steps.get(step_id)


@dataclass
class WorkflowExecution:
    """Tracks the execution of a workflow."""
    id: str
    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    results: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    step_history: List[Dict[str, Any]] = field(default_factory=list)


class WorkflowEngine:
    """
    Executes and manages workflows.

    Features:
    - Register workflow definitions
    - Execute workflows with context
    - Support for conditional branching
    - Support for parallel execution
    - Subprocess workflows
    - Execution tracking and history
    """

    def __init__(self):
        """Initialize the WorkflowEngine."""
        self._workflows: Dict[str, Workflow] = {}
        self._executions: Dict[str, WorkflowExecution] = {}
        self._execution_history: List[WorkflowExecution] = []
        self._global_actions: Dict[str, Callable] = {}

    def register(
        self,
        workflow: Workflow = None,
        workflow_id: Optional[str] = None,
        name: Optional[str] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Workflow:
        """
        Register a workflow.

        Args:
            workflow: Optional pre-built Workflow object
            workflow_id: Optional custom ID (if not providing workflow)
            name: Workflow name (if not providing workflow)
            description: Workflow description
            tags: Optional tags
            metadata: Optional metadata

        Returns:
            The registered Workflow
        """
        if workflow is None:
            workflow_id = workflow_id or f"wf_{secrets.token_hex(6)}"
            name = name or f"Workflow {workflow_id}"

            workflow = Workflow(
                id=workflow_id,
                name=name,
                description=description,
                tags=tags or [],
                metadata=metadata or {}
            )

        if workflow.id in self._workflows:
            raise ValueError(f"Workflow with ID '{workflow.id}' already exists")

        self._workflows[workflow.id] = workflow
        return workflow

    def unregister(self, workflow_id: str) -> bool:
        """
        Unregister a workflow.

        Args:
            workflow_id: The workflow ID

        Returns:
            True if removed, False if not found
        """
        if workflow_id in self._workflows:
            del self._workflows[workflow_id]
            return True
        return False

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """
        Get a workflow by ID.

        Args:
            workflow_id: The workflow ID

        Returns:
            The workflow or None
        """
        return self._workflows.get(workflow_id)

    def list_workflows(
        self,
        enabled_only: bool = False,
        tags: Optional[List[str]] = None
    ) -> List[Workflow]:
        """
        List registered workflows.

        Args:
            enabled_only: Only return enabled workflows
            tags: Optional filter by tags (any match)

        Returns:
            List of matching workflows
        """
        workflows = list(self._workflows.values())

        if enabled_only:
            workflows = [w for w in workflows if w.enabled]

        if tags:
            workflows = [w for w in workflows if any(t in w.tags for t in tags)]

        return workflows

    def run(
        self,
        workflow_id: str,
        context: Optional[Dict[str, Any]] = None,
        execution_id: Optional[str] = None
    ) -> WorkflowExecution:
        """
        Execute a workflow.

        Args:
            workflow_id: The workflow to execute
            context: Optional initial context/input data
            execution_id: Optional custom execution ID

        Returns:
            The workflow execution record

        Raises:
            ValueError: If workflow not found or has no steps
        """
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        if not workflow.steps:
            raise ValueError(f"Workflow '{workflow_id}' has no steps")

        if not workflow.entry_point:
            raise ValueError(f"Workflow '{workflow_id}' has no entry point")

        execution_id = execution_id or f"exec_{secrets.token_hex(8)}"

        execution = WorkflowExecution(
            id=execution_id,
            workflow_id=workflow_id,
            context=copy.deepcopy(context or {}),
            current_step=workflow.entry_point,
            status=WorkflowStatus.RUNNING,
            started_at=datetime.now()
        )

        self._executions[execution_id] = execution

        # Execute the workflow
        try:
            self._execute_workflow(workflow, execution)
        except Exception as e:
            execution.status = WorkflowStatus.FAILED
            execution.error = str(e)

        execution.completed_at = datetime.now()
        self._execution_history.append(execution)

        return execution

    def _execute_workflow(
        self,
        workflow: Workflow,
        execution: WorkflowExecution
    ) -> None:
        """Execute workflow steps."""
        max_steps = 1000  # Prevent infinite loops
        step_count = 0

        while execution.current_step and step_count < max_steps:
            step_count += 1
            step = workflow.get_step(execution.current_step)

            if not step:
                raise ValueError(f"Step '{execution.current_step}' not found")

            # Record step entry
            step_record = {
                "step_id": step.id,
                "step_name": step.name,
                "started_at": datetime.now().isoformat(),
                "result": None,
                "error": None
            }

            try:
                next_step = self._execute_step(step, execution)
                step_record["result"] = execution.results.get(step.id)
                step_record["next_step"] = next_step
                execution.current_step = next_step

            except Exception as e:
                step_record["error"] = str(e)

                # Retry logic
                if step.retry_count < step.max_retries:
                    step.retry_count += 1
                    step_record["retry"] = step.retry_count
                    continue
                else:
                    raise

            finally:
                step_record["completed_at"] = datetime.now().isoformat()
                execution.step_history.append(step_record)

        if step_count >= max_steps:
            raise RuntimeError("Maximum step count exceeded - possible infinite loop")

        execution.status = WorkflowStatus.COMPLETED

    def _execute_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """
        Execute a single workflow step.

        Returns:
            The ID of the next step, or None if workflow is complete
        """
        if step.step_type == StepType.ACTION:
            return self._execute_action_step(step, execution)

        elif step.step_type == StepType.CONDITION:
            return self._execute_condition_step(step, execution)

        elif step.step_type == StepType.TRANSFORM:
            return self._execute_transform_step(step, execution)

        elif step.step_type == StepType.DELAY:
            return self._execute_delay_step(step, execution)

        elif step.step_type == StepType.PARALLEL:
            return self._execute_parallel_step(step, execution)

        elif step.step_type == StepType.SUBPROCESS:
            return self._execute_subprocess_step(step, execution)

        else:
            raise ValueError(f"Unknown step type: {step.step_type}")

    def _execute_action_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute an action step."""
        if step.action:
            result = step.action(execution.context)
            execution.results[step.id] = result

            # Update context if result is a dict
            if isinstance(result, dict):
                execution.context.update(result)

        return step.next_step

    def _execute_condition_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute a condition step."""
        if step.condition:
            result = step.condition(execution.context)
            execution.results[step.id] = result

            if result:
                return step.on_true
            else:
                return step.on_false

        return step.next_step

    def _execute_transform_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute a transform step."""
        if step.transform:
            # Get input from context or previous results
            input_data = execution.context.get("_transform_input", execution.context)
            result = step.transform(input_data)
            execution.results[step.id] = result

            # Update context
            if isinstance(result, dict):
                execution.context.update(result)
            else:
                execution.context["_transform_output"] = result

        return step.next_step

    def _execute_delay_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute a delay step."""
        import time

        if step.delay_seconds > 0:
            # In a real implementation, this might be async
            # For testing, we just record it
            execution.results[step.id] = {
                "delayed": True,
                "seconds": step.delay_seconds
            }
            # Simulate a small delay for testing
            time.sleep(min(step.delay_seconds, 0.1))

        return step.next_step

    def _execute_parallel_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute parallel steps."""
        workflow = self._workflows.get(execution.workflow_id)
        if not workflow:
            raise ValueError("Workflow not found")

        results = {}
        for parallel_step_id in step.parallel_steps:
            parallel_step = workflow.get_step(parallel_step_id)
            if parallel_step:
                # Execute each parallel step
                # In a real implementation, this would be concurrent
                try:
                    self._execute_step(parallel_step, execution)
                    results[parallel_step_id] = execution.results.get(parallel_step_id)
                except Exception as e:
                    results[parallel_step_id] = {"error": str(e)}

        execution.results[step.id] = results
        return step.next_step

    def _execute_subprocess_step(
        self,
        step: WorkflowStep,
        execution: WorkflowExecution
    ) -> Optional[str]:
        """Execute a subprocess (nested workflow)."""
        if step.subprocess_id:
            subprocess_result = self.run(
                step.subprocess_id,
                context=execution.context.copy()
            )
            execution.results[step.id] = {
                "subprocess_id": subprocess_result.id,
                "status": subprocess_result.status.value,
                "results": subprocess_result.results
            }

            # Merge subprocess context back
            execution.context.update(subprocess_result.context)

        return step.next_step

    def pause_execution(self, execution_id: str) -> bool:
        """
        Pause a running execution.

        Args:
            execution_id: The execution ID

        Returns:
            True if paused, False if not found or not running
        """
        if execution_id in self._executions:
            execution = self._executions[execution_id]
            if execution.status == WorkflowStatus.RUNNING:
                execution.status = WorkflowStatus.PAUSED
                return True
        return False

    def cancel_execution(self, execution_id: str) -> bool:
        """
        Cancel an execution.

        Args:
            execution_id: The execution ID

        Returns:
            True if cancelled, False if not found
        """
        if execution_id in self._executions:
            execution = self._executions[execution_id]
            if execution.status in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
                execution.status = WorkflowStatus.CANCELLED
                execution.completed_at = datetime.now()
                return True
        return False

    def get_execution(self, execution_id: str) -> Optional[WorkflowExecution]:
        """
        Get an execution by ID.

        Args:
            execution_id: The execution ID

        Returns:
            The execution or None
        """
        return self._executions.get(execution_id)

    def list_executions(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[WorkflowStatus] = None
    ) -> List[WorkflowExecution]:
        """
        List workflow executions.

        Args:
            workflow_id: Optional filter by workflow
            status: Optional filter by status

        Returns:
            List of matching executions
        """
        executions = list(self._executions.values())

        if workflow_id:
            executions = [e for e in executions if e.workflow_id == workflow_id]

        if status:
            executions = [e for e in executions if e.status == status]

        return executions

    def register_global_action(
        self,
        name: str,
        action: Callable[[Dict[str, Any]], Any]
    ) -> None:
        """
        Register a global action that can be used in workflows.

        Args:
            name: The action name
            action: The action function
        """
        self._global_actions[name] = action

    def get_global_action(self, name: str) -> Optional[Callable]:
        """Get a registered global action."""
        return self._global_actions.get(name)

    def create_step(
        self,
        step_id: str,
        name: str,
        step_type: StepType = StepType.ACTION,
        action: Optional[Callable] = None,
        condition: Optional[Callable] = None,
        transform: Optional[Callable] = None,
        next_step: Optional[str] = None,
        on_true: Optional[str] = None,
        on_false: Optional[str] = None,
        delay_seconds: float = 0,
        parallel_steps: Optional[List[str]] = None,
        subprocess_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> WorkflowStep:
        """
        Factory method to create a workflow step.

        Args:
            step_id: Unique step ID
            name: Step name
            step_type: Type of step
            action: Action function (for ACTION type)
            condition: Condition function (for CONDITION type)
            transform: Transform function (for TRANSFORM type)
            next_step: Default next step
            on_true: Next step if condition is true
            on_false: Next step if condition is false
            delay_seconds: Delay in seconds (for DELAY type)
            parallel_steps: List of step IDs (for PARALLEL type)
            subprocess_id: Workflow ID (for SUBPROCESS type)
            metadata: Optional metadata

        Returns:
            The created WorkflowStep
        """
        return WorkflowStep(
            id=step_id,
            name=name,
            step_type=step_type,
            action=action,
            condition=condition,
            transform=transform,
            next_step=next_step,
            on_true=on_true,
            on_false=on_false,
            delay_seconds=delay_seconds,
            parallel_steps=parallel_steps or [],
            subprocess_id=subprocess_id,
            metadata=metadata or {}
        )
