"""
Parallel Agent Dispatch — bridges SmartLedger task graph to concurrent execution.

Uses the existing SmartLedger primitives:
- get_parallel_executable_tasks() — returns all tasks ready to run NOW
- get_next_executable_task() — respects dependencies, priorities, insertion order
- complete_task_and_route() — auto-unblocks dependents on completion

ThreadPoolExecutor pattern reused from speculative_dispatcher.py.
"""
import atexit
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve_social')

_executor = None
_executor_lock = threading.Lock()
MAX_PARALLEL_WORKERS = int(os.environ.get('HEVOLVE_PARALLEL_WORKERS', '8'))


def get_executor() -> ThreadPoolExecutor:
    """Lazy singleton ThreadPoolExecutor."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=MAX_PARALLEL_WORKERS,
                    thread_name_prefix='hart-parallel')
                atexit.register(lambda: _executor.shutdown(wait=False))
    return _executor


def dispatch_parallel_tasks(ledger, dispatch_fn: Callable,
                            max_concurrent: int = 8) -> Dict:
    """Fan-out all parallel-ready tasks from the ledger to concurrent workers.

    Args:
        ledger: SmartLedger instance with tasks loaded
        dispatch_fn: Callable(task) -> result dict (e.g., calls /chat)
        max_concurrent: Max simultaneous dispatches

    Returns:
        {completed: int, failed: int, results: {task_id: result}}
    """
    from agent_ledger.core import TaskStatus

    parallel_tasks = ledger.get_parallel_executable_tasks()
    if not parallel_tasks:
        return {'completed': 0, 'failed': 0, 'results': {}}

    batch = parallel_tasks[:max_concurrent]
    executor = get_executor()
    results = {}
    futures = {}

    # Mark all as IN_PROGRESS before dispatch
    for task in batch:
        ledger.update_task_status(task.task_id, TaskStatus.IN_PROGRESS)
        future = executor.submit(dispatch_fn, task)
        futures[future] = task

    completed = 0
    failed = 0
    for future in as_completed(futures):
        task = futures[future]
        try:
            result = future.result(timeout=300)
            if result.get('success', True):
                # Use update_task_status to trigger _handle_task_completion
                # which properly unblocks dependent tasks
                ledger.update_task_status(
                    task.task_id, TaskStatus.COMPLETED, result=result)
                completed += 1
            else:
                ledger.update_task_status(
                    task.task_id, TaskStatus.FAILED,
                    error_message=str(result.get('error', 'failed')))
                failed += 1
            results[task.task_id] = result
        except Exception as e:
            logger.warning(f"Parallel task {task.task_id} failed: {e}")
            ledger.update_task_status(
                task.task_id, TaskStatus.FAILED, error_message=str(e))
            results[task.task_id] = {'error': str(e)}
            failed += 1

    ledger.save()
    return {'completed': completed, 'failed': failed, 'results': results}


def dispatch_goal_with_ledger(ledger, dispatch_fn: Callable) -> Dict:
    """Execute ALL tasks in a ledger, respecting parallel/sequential ordering.

    Loops until no more executable tasks remain:
    1. Get parallel-ready tasks -> fan-out
    2. Get next sequential task -> execute
    3. complete_task_and_route() auto-unblocks dependents
    4. Repeat until done
    """
    from agent_ledger.core import TaskStatus

    total_completed = 0
    total_failed = 0
    all_results = {}
    max_iterations = 100  # Safety cap

    for _ in range(max_iterations):
        # Try parallel batch first
        parallel = ledger.get_parallel_executable_tasks()
        if parallel:
            batch_result = dispatch_parallel_tasks(
                ledger, dispatch_fn, max_concurrent=MAX_PARALLEL_WORKERS)
            total_completed += batch_result['completed']
            total_failed += batch_result['failed']
            all_results.update(batch_result['results'])
            continue

        # Fall back to sequential
        next_task = ledger.get_next_executable_task()
        if not next_task:
            break

        ledger.update_task_status(next_task.task_id, TaskStatus.IN_PROGRESS)
        try:
            result = dispatch_fn(next_task)
            if result.get('success', True):
                ledger.update_task_status(
                    next_task.task_id, TaskStatus.COMPLETED, result=result)
                total_completed += 1
            else:
                ledger.update_task_status(
                    next_task.task_id, TaskStatus.FAILED,
                    error_message=str(result.get('error', 'failed')))
                total_failed += 1
            all_results[next_task.task_id] = result
        except Exception as e:
            logger.warning(f"Sequential task {next_task.task_id} failed: {e}")
            ledger.update_task_status(
                next_task.task_id, TaskStatus.FAILED, error_message=str(e))
            all_results[next_task.task_id] = {'error': str(e)}
            total_failed += 1

    ledger.save()
    return {
        'completed': total_completed,
        'failed': total_failed,
        'results': all_results,
        'awareness': ledger.get_awareness(),
    }


def decompose_goal_to_ledger(prompt: str, goal_id: str, goal_type: str,
                             user_id: str, subtask_defs: Optional[Dict] = None):
    """Decompose a goal into a SmartLedger with parallel/sequential tasks.

    Args:
        prompt: Goal prompt text
        goal_id: Goal identifier
        goal_type: Goal type (marketing, coding, etc.)
        user_id: Owner user ID
        subtask_defs: Optional dict with 'tasks' list and 'parallel' bool.
            If None, creates a single root task (backward-compatible).

    Returns:
        (task_list, ledger) — task_list for coordinator compatibility,
        ledger for parallel dispatch. ledger is None for single-task goals.
    """
    try:
        from agent_ledger import SmartLedger, Task, TaskType, ExecutionMode

        ledger = SmartLedger(agent_id=user_id, session_id=str(goal_id))

        # Create root task (the goal itself)
        root = Task(
            task_id=f'{goal_id}_root',
            description=prompt[:500],
            task_type=TaskType.AUTONOMOUS,
            execution_mode=ExecutionMode.SEQUENTIAL,
        )
        ledger.add_task(root)

        if subtask_defs and isinstance(subtask_defs.get('tasks'), list) \
                and len(subtask_defs['tasks']) > 1:
            is_parallel = subtask_defs.get('parallel', False)
            tasks = subtask_defs['tasks']

            if is_parallel:
                # Fan-out: create sibling tasks under root
                siblings = ledger.create_sibling_tasks(
                    parent_task_id=root.task_id,
                    sibling_descriptions=[
                        t.get('description', t) if isinstance(t, dict)
                        else str(t)
                        for t in tasks
                    ],
                    task_type=TaskType.PRE_ASSIGNED,
                )
                # Mark siblings as PARALLEL + ready
                # (create_sibling_tasks defaults to SEQUENTIAL, pending_reason=None)
                for sib in siblings:
                    sib.execution_mode = ExecutionMode.PARALLEL
                    sib.pending_reason = 'ready'
                    # Ensure in task_order (create_sibling_tasks skips add_task)
                    if sib.task_id not in ledger.task_order:
                        ledger.task_order.append(sib.task_id)
            else:
                # Sequential chain under root
                seq_tasks = ledger.create_sequential_tasks(
                    [t.get('description', t) if isinstance(t, dict)
                     else str(t)
                     for t in tasks],
                    task_type=TaskType.PRE_ASSIGNED,
                    parent_task_id=root.task_id,
                )
                # Ensure in task_order (create_sequential_tasks skips add_task)
                for st in seq_tasks:
                    if st.task_id not in ledger.task_order:
                        ledger.task_order.append(st.task_id)

            ledger.save()

            # Convert to coordinator-compatible dicts
            result = []
            for tid in ledger.task_order:
                task = ledger.tasks[tid]
                result.append({
                    'task_id': tid,
                    'description': task.description[:500],
                    'capabilities': [goal_type],
                    'execution_mode': task.execution_mode.value
                    if hasattr(task.execution_mode, 'value')
                    else str(task.execution_mode),
                    'prerequisites': list(task.prerequisites),
                })
            return result, ledger

        # Single task — no ledger needed for parallel dispatch
        return [{
            'task_id': f'{goal_id}_task_0',
            'description': prompt[:500],
            'capabilities': [goal_type],
        }], None

    except ImportError:
        # Fallback: single-task decomposition
        return [{
            'task_id': f'{goal_id}_task_0',
            'description': prompt[:500],
            'capabilities': [goal_type],
        }], None


def extract_subtasks_from_context(goal_id: str) -> Optional[Dict]:
    """Check if the goal has explicit subtask definitions in its context.

    AgentGoal.context can contain:
        {"tasks": [{"description": "..."}, ...], "parallel": true/false}

    Returns the context dict if subtasks are present, None otherwise.
    """
    try:
        from integrations.social.models import get_db, AgentGoal
        db = get_db()
        try:
            goal = db.query(AgentGoal).filter_by(id=goal_id).first()
            if goal and goal.context:
                ctx = json.loads(goal.context) if isinstance(
                    goal.context, str) else goal.context
                if isinstance(ctx, dict) and 'tasks' in ctx:
                    return ctx
        finally:
            db.close()
    except Exception:
        pass
    return None
