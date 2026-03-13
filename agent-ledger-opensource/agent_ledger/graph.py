"""
Task Graph and State Machine for Agent Ledger

Provides graph-based task dependency visualization and state machine
validation for task transitions.

Features:
- DAG (Directed Acyclic Graph) representation of task dependencies
- State machine for valid task status transitions
- Topological sorting for execution order
- Cycle detection in dependencies
- Parallel execution grouping
"""

from typing import Dict, List, Set, Optional, Tuple, Any
from collections import deque, defaultdict

from .core import Task, TaskStatus, ExecutionMode, SmartLedger


class TaskGraph:
    """
    Directed Acyclic Graph (DAG) representation of task dependencies.

    Provides:
    - Topological sorting for execution order
    - Cycle detection
    - Critical path analysis
    - Parallel execution grouping
    """

    def __init__(self, ledger: SmartLedger):
        """
        Initialize task graph from ledger.

        Args:
            ledger: SmartLedger instance containing tasks
        """
        self.ledger = ledger
        self.adjacency: Dict[str, Set[str]] = defaultdict(set)
        self.reverse_adjacency: Dict[str, Set[str]] = defaultdict(set)
        self._build_graph()

    def _build_graph(self):
        """Build adjacency lists from task prerequisites."""
        for task_id, task in self.ledger.tasks.items():
            for prereq_id in task.prerequisites:
                self.adjacency[prereq_id].add(task_id)
                self.reverse_adjacency[task_id].add(prereq_id)

            if task_id not in self.adjacency:
                self.adjacency[task_id] = set()
            if task_id not in self.reverse_adjacency:
                self.reverse_adjacency[task_id] = set()

    def detect_cycles(self) -> Optional[List[str]]:
        """
        Detect cycles in the task dependency graph.

        Returns:
            List of task IDs forming a cycle, or None if no cycle exists
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {task_id: WHITE for task_id in self.ledger.tasks}
        parent = {}

        def dfs(node: str) -> Optional[List[str]]:
            color[node] = GRAY

            for neighbor in self.adjacency[node]:
                if color[neighbor] == WHITE:
                    parent[neighbor] = node
                    cycle = dfs(neighbor)
                    if cycle:
                        return cycle
                elif color[neighbor] == GRAY:
                    cycle = [neighbor]
                    current = node
                    while current != neighbor:
                        cycle.append(current)
                        current = parent[current]
                    cycle.append(neighbor)
                    return list(reversed(cycle))

            color[node] = BLACK
            return None

        for task_id in self.ledger.tasks:
            if color[task_id] == WHITE:
                cycle = dfs(task_id)
                if cycle:
                    return cycle

        return None

    def topological_sort(self) -> List[str]:
        """
        Get topological ordering of tasks (valid execution order).

        Returns:
            List of task IDs in valid execution order

        Raises:
            ValueError: If graph contains a cycle
        """
        cycle = self.detect_cycles()
        if cycle:
            raise ValueError(f"Cannot sort: cycle detected in tasks: {' -> '.join(cycle)}")

        in_degree = {task_id: len(self.reverse_adjacency[task_id]) for task_id in self.ledger.tasks}
        queue = deque([task_id for task_id, degree in in_degree.items() if degree == 0])
        sorted_tasks = []

        while queue:
            task_id = queue.popleft()
            sorted_tasks.append(task_id)

            for dependent_id in self.adjacency[task_id]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        return sorted_tasks

    def get_parallel_groups(self) -> List[List[str]]:
        """
        Group tasks by execution level (tasks that can run in parallel).

        Returns:
            List of groups, where each group contains task IDs that can execute concurrently
        """
        sorted_tasks = self.topological_sort()
        levels: Dict[str, int] = {}

        for task_id in sorted_tasks:
            if not self.reverse_adjacency[task_id]:
                levels[task_id] = 0
            else:
                levels[task_id] = max(levels[prereq_id] for prereq_id in self.reverse_adjacency[task_id]) + 1

        max_level = max(levels.values()) if levels else 0
        groups = [[] for _ in range(max_level + 1)]

        for task_id, level in levels.items():
            task = self.ledger.get_task(task_id)
            if task and task.execution_mode == ExecutionMode.PARALLEL:
                groups[level].append(task_id)
            elif task:
                groups[level].append(task_id)

        return [group for group in groups if group]

    def get_critical_path(self) -> Tuple[List[str], int]:
        """
        Find critical path (longest path through graph).

        Useful for estimating minimum completion time.

        Returns:
            Tuple of (task IDs in critical path, total duration estimate)
        """
        sorted_tasks = self.topological_sort()
        distance = {task_id: 0 for task_id in self.ledger.tasks}
        parent = {task_id: None for task_id in self.ledger.tasks}

        for task_id in sorted_tasks:
            task = self.ledger.get_task(task_id)
            duration = task.context.get("estimated_duration", 1) if task else 1

            for dependent_id in self.adjacency[task_id]:
                new_distance = distance[task_id] + duration
                if new_distance > distance[dependent_id]:
                    distance[dependent_id] = new_distance
                    parent[dependent_id] = task_id

        end_node = max(distance, key=distance.get)

        path = []
        current = end_node
        while current is not None:
            path.append(current)
            current = parent[current]

        path.reverse()
        total_duration = distance[end_node]

        return path, total_duration

    def visualize_ascii(self) -> str:
        """
        Create ASCII art visualization of task graph.

        Returns:
            String containing ASCII representation
        """
        try:
            sorted_tasks = self.topological_sort()
        except ValueError as e:
            return f"Cannot visualize: {e}"

        groups = self.get_parallel_groups()

        lines = [
            "",
            "=" * 60,
            "TASK DEPENDENCY GRAPH",
            "=" * 60,
            ""
        ]

        for level_idx, group in enumerate(groups):
            lines.append(f"Level {level_idx}:")
            for task_id in group:
                task = self.ledger.get_task(task_id)
                if task:
                    status_icon = {
                        TaskStatus.PENDING: "[ ]",
                        TaskStatus.IN_PROGRESS: "[>]",
                        TaskStatus.COMPLETED: "[x]",
                        TaskStatus.BLOCKED: "[!]",
                        TaskStatus.FAILED: "[X]"
                    }.get(task.status, "[ ]")

                    mode_icon = "||" if task.execution_mode == ExecutionMode.PARALLEL else "--"
                    lines.append(f"  {mode_icon} {status_icon} {task.description}")

                    if task.prerequisites:
                        prereq_names = [
                            self.ledger.get_task(pid).description[:30] if self.ledger.get_task(pid) else pid
                            for pid in task.prerequisites
                        ]
                        lines.append(f"     +-- Depends on: {', '.join(prereq_names)}")

            lines.append("")

        return "\n".join(lines)


class TaskStateMachine:
    """
    State machine for validating task status transitions.

    Ensures tasks follow valid state transition paths and
    provides hooks for state change callbacks.
    """

    TRANSITIONS = {
        TaskStatus.PENDING: [
            TaskStatus.IN_PROGRESS, TaskStatus.PAUSED, TaskStatus.CANCELLED,
            TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE, TaskStatus.DEFERRED,
            TaskStatus.DELEGATED,
        ],
        TaskStatus.DEFERRED: [
            TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED,
            TaskStatus.SKIPPED, TaskStatus.NOT_APPLICABLE,
        ],
        TaskStatus.IN_PROGRESS: [
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED,
            TaskStatus.USER_STOPPED, TaskStatus.BLOCKED, TaskStatus.TERMINATED,
            TaskStatus.NOT_APPLICABLE, TaskStatus.DELEGATED,
        ],
        TaskStatus.DELEGATED: [
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED, TaskStatus.BLOCKED,
        ],
        TaskStatus.PAUSED: [
            TaskStatus.RESUMING, TaskStatus.CANCELLED, TaskStatus.TERMINATED,
            TaskStatus.NOT_APPLICABLE, TaskStatus.SKIPPED, TaskStatus.DEFERRED,
        ],
        TaskStatus.USER_STOPPED: [
            TaskStatus.RESUMING, TaskStatus.CANCELLED, TaskStatus.TERMINATED,
            TaskStatus.NOT_APPLICABLE, TaskStatus.SKIPPED, TaskStatus.DEFERRED,
        ],
        TaskStatus.BLOCKED: [
            TaskStatus.PENDING, TaskStatus.RESUMING, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.NOT_APPLICABLE, TaskStatus.DEFERRED,
        ],
        TaskStatus.RESUMING: [
            TaskStatus.IN_PROGRESS, TaskStatus.PAUSED, TaskStatus.FAILED,
        ],
        # Terminal states — no transitions out (except COMPLETED → ROLLED_BACK)
        TaskStatus.COMPLETED: [TaskStatus.ROLLED_BACK],
        TaskStatus.FAILED: [],
        TaskStatus.CANCELLED: [],
        TaskStatus.TERMINATED: [],
        TaskStatus.SKIPPED: [],
        TaskStatus.NOT_APPLICABLE: [],
        TaskStatus.ROLLED_BACK: [],
    }

    @classmethod
    def is_valid_transition(cls, from_status: TaskStatus, to_status: TaskStatus) -> bool:
        """
        Check if a status transition is valid.

        Delegates to Task._validate_transition() logic via the TRANSITIONS dict
        which covers all 15 TaskStatus states.

        Args:
            from_status: Current status
            to_status: Target status

        Returns:
            True if transition is allowed, False otherwise
        """
        if from_status == to_status:
            return True

        return to_status in cls.TRANSITIONS.get(from_status, [])

    @classmethod
    def get_allowed_transitions(cls, current_status: TaskStatus) -> List[TaskStatus]:
        """
        Get list of allowed transitions from current status.

        Args:
            current_status: Current task status

        Returns:
            List of valid next statuses
        """
        return cls.TRANSITIONS.get(current_status, [])

    @classmethod
    def validate_ledger_transitions(cls, ledger: SmartLedger) -> List[str]:
        """
        Validate all tasks in ledger have valid status transitions.

        Args:
            ledger: SmartLedger to validate

        Returns:
            List of error messages (empty if all valid)
        """
        errors = []

        for task_id, task in ledger.tasks.items():
            if task.status == TaskStatus.IN_PROGRESS:
                for prereq_id in task.prerequisites:
                    prereq = ledger.get_task(prereq_id)
                    if not prereq:
                        errors.append(f"Task {task_id}: prerequisite {prereq_id} not found")
                    elif prereq.status != TaskStatus.COMPLETED:
                        errors.append(f"Task {task_id}: prerequisite {prereq_id} not completed (status: {prereq.status})")

        return errors


def analyze_ledger(ledger: SmartLedger) -> Dict[str, Any]:
    """
    Comprehensive analysis of task ledger.

    Args:
        ledger: SmartLedger to analyze

    Returns:
        Dictionary containing:
        - graph: Task graph analysis
        - state_machine: State validation results
        - recommendations: Suggested actions
    """
    graph = TaskGraph(ledger)

    cycle = graph.detect_cycles()
    has_cycle = cycle is not None

    try:
        execution_order = graph.topological_sort()
        can_execute = True
    except ValueError:
        execution_order = []
        can_execute = False

    parallel_groups = graph.get_parallel_groups() if can_execute else []

    if can_execute:
        critical_path, critical_duration = graph.get_critical_path()
    else:
        critical_path, critical_duration = [], 0

    state_errors = TaskStateMachine.validate_ledger_transitions(ledger)

    recommendations = []
    if has_cycle:
        recommendations.append(f"Fix circular dependency: {' -> '.join(cycle)}")
    if state_errors:
        recommendations.extend(state_errors)
    if not parallel_groups:
        recommendations.append("Consider marking independent tasks as PARALLEL for faster execution")

    return {
        "graph": {
            "has_cycle": has_cycle,
            "cycle": cycle,
            "can_execute": can_execute,
            "execution_order": execution_order,
            "parallel_groups": parallel_groups,
            "critical_path": critical_path,
            "critical_duration": critical_duration
        },
        "state_machine": {
            "valid": len(state_errors) == 0,
            "errors": state_errors
        },
        "recommendations": recommendations
    }
