"""
Claude Code Hive Session — Connect Claude Code to the HART OS hive.

When a user opts in, their Claude Code session becomes a hive worker:
  - Receives coding tasks from the hive's task distributor
  - Executes them using Claude Code's native capabilities
  - Returns results through PeerLink
  - Earns Spark tokens for contributions

Protocol:
  1. User runs: hart hive connect (or POST /api/hive/session/connect)
  2. Session registers with PeerLink as a CODING_AGENT peer
  3. Hive dispatcher sees this node as available for coding tasks
  4. Tasks arrive via instruction queue (privacy-filtered through shard engine)
  5. Claude Code executes: read files, edit code, run tests
  6. Results published back via PeerLink + EventBus
  7. Spark tokens awarded based on task complexity and quality

Security:
  - Shard engine filters what code is shared (INTERFACES level for untrusted peers)
  - User can set scope: own repos only, public repos, or any hive task
  - All code changes require user approval before commit
  - Master key verification on task origin (no rogue dispatchers)
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.hive_session')

# ─── Constants ───────────────────────────────────────────────────────

TASK_SCOPES = {'own_repos', 'public', 'any'}

SESSION_CAPABILITIES_DEFAULT = {
    'languages': ['python', 'javascript', 'typescript', 'rust', 'go'],
    'frameworks': ['flask', 'fastapi', 'react', 'next.js'],
    'can_run_tests': True,
    'can_edit_files': True,
    'can_read_files': True,
    'can_run_commands': True,
    'max_file_size_kb': 500,
    'max_task_duration_minutes': 30,
}

PEER_TYPE = 'CODING_AGENT'

# Session status values
STATUS_DISCONNECTED = 'disconnected'
STATUS_CONNECTING = 'connecting'
STATUS_IDLE = 'idle'
STATUS_WORKING = 'working'
STATUS_PAUSED = 'paused'

_VALID_STATUSES = {
    STATUS_DISCONNECTED, STATUS_CONNECTING, STATUS_IDLE,
    STATUS_WORKING, STATUS_PAUSED,
}

# EventBus topic for hive task dispatch
EVENT_TASK_DISPATCHED = 'hive.task.dispatched'
EVENT_TASK_COMPLETED = 'hive.task.completed'
EVENT_SESSION_CONNECTED = 'hive.session.connected'
EVENT_SESSION_DISCONNECTED = 'hive.session.disconnected'

# Spark reward constants (per-task base, scaled by complexity)
SPARK_BASE_REWARD = 10
SPARK_COMPLEXITY_MULTIPLIER = 5
SPARK_QUALITY_BONUS_THRESHOLD = 0.8  # quality score above this earns bonus

# Max pending tasks before rejecting new ones
MAX_PENDING_TASKS = 20


class ClaudeHiveSession:
    """Manages a Claude Code session's connection to the HART OS hive.

    Thread-safe. All public methods acquire self._lock before mutating state.
    """

    def __init__(self):
        self.session_id: str = ''
        self.user_id: str = ''
        self.status: str = STATUS_DISCONNECTED
        self.capabilities: Dict[str, Any] = {}
        self.task_scope: str = 'own_repos'
        self.current_task: Optional[Dict] = None
        self.stats: Dict[str, Any] = {
            'tasks_completed': 0,
            'tasks_failed': 0,
            'spark_earned': 0,
            'avg_quality_score': 0.0,
            'total_quality_points': 0.0,
            'connected_since': None,
        }

        self._peer_link_id: Optional[str] = None
        self._instruction_queue = None  # Lazy: InstructionQueue from instruction_queue.py
        self._lock = threading.Lock()
        self._event_subscriptions: List = []
        self._pending_tasks: List[Dict] = []
        self._completed_tasks: List[Dict] = []
        self._max_completed_history = 100

    # ─── Connection Lifecycle ────────────────────────────────────────

    def connect(self, user_id: str, capabilities: Dict = None,
                task_scope: str = 'own_repos') -> Dict:
        """Register with PeerLink as a CODING_AGENT worker node.

        Subscribes to task dispatch events on EventBus so the hive
        dispatcher can route coding tasks to this session.

        Args:
            user_id: HART OS user ID
            capabilities: What this session can do (languages, etc.)
            task_scope: 'own_repos', 'public', or 'any'

        Returns:
            Session info dict with session_id, status, peer_link_id
        """
        if task_scope not in TASK_SCOPES:
            return {
                'success': False,
                'error': f'Invalid task_scope: {task_scope}. Must be one of {TASK_SCOPES}',
            }

        with self._lock:
            if self.status != STATUS_DISCONNECTED:
                return {
                    'success': False,
                    'error': f'Already connected (status={self.status})',
                    'session_id': self.session_id,
                }

            self.status = STATUS_CONNECTING
            self.user_id = user_id
            self.session_id = f'chs_{uuid.uuid4().hex[:12]}'
            self.capabilities = capabilities or dict(SESSION_CAPABILITIES_DEFAULT)
            self.task_scope = task_scope
            self.stats['connected_since'] = time.time()

        # Register with PeerLink
        peer_link_id = self._register_peer_link()

        # Subscribe to EventBus for task dispatch
        self._subscribe_events()

        # Initialize local instruction queue
        self._init_instruction_queue()

        with self._lock:
            self._peer_link_id = peer_link_id
            self.status = STATUS_IDLE

        # Emit connection event
        self._emit_event(EVENT_SESSION_CONNECTED, {
            'session_id': self.session_id,
            'user_id': self.user_id,
            'capabilities': self.capabilities,
            'task_scope': self.task_scope,
            'peer_type': PEER_TYPE,
        })

        logger.info(
            "Hive session connected: session=%s user=%s scope=%s peer=%s",
            self.session_id, self.user_id, self.task_scope,
            peer_link_id or 'local-only',
        )

        return {
            'success': True,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'status': STATUS_IDLE,
            'peer_link_id': peer_link_id,
            'capabilities': self.capabilities,
            'task_scope': self.task_scope,
        }

    def disconnect(self) -> Dict:
        """Unregister from PeerLink, unsubscribe events, flush tasks.

        Returns:
            Summary of session including final stats
        """
        with self._lock:
            if self.status == STATUS_DISCONNECTED:
                return {'success': True, 'message': 'Already disconnected'}

            prev_status = self.status
            self.status = STATUS_DISCONNECTED
            session_id = self.session_id
            final_stats = dict(self.stats)
            pending_count = len(self._pending_tasks)

        # Unsubscribe from EventBus
        self._unsubscribe_events()

        # Unregister from PeerLink
        self._unregister_peer_link()

        # Flush pending tasks back to hive queue
        if pending_count > 0:
            self._flush_pending_tasks()

        # Emit disconnection event
        self._emit_event(EVENT_SESSION_DISCONNECTED, {
            'session_id': session_id,
            'user_id': self.user_id,
            'final_stats': final_stats,
        })

        with self._lock:
            self._peer_link_id = None
            self.current_task = None
            self.session_id = ''

        logger.info(
            "Hive session disconnected: session=%s stats=%s flushed=%d",
            session_id, final_stats, pending_count,
        )

        return {
            'success': True,
            'session_id': session_id,
            'previous_status': prev_status,
            'stats': final_stats,
            'flushed_tasks': pending_count,
        }

    # ─── Task Reception ──────────────────────────────────────────────

    def receive_task(self, task: Dict) -> bool:
        """Accept a coding task from the hive dispatcher.

        Validates:
          1. Session is idle or has room in pending queue
          2. Task origin signature (master key verification)
          3. Task scope matches session scope setting
          4. Privacy filtering via shard engine

        Args:
            task: Dict with keys: task_id, description, origin_signature,
                  scope_level, target_files, test_expectations, etc.

        Returns:
            True if task was accepted and queued
        """
        task_id = task.get('task_id', 'unknown')

        with self._lock:
            if self.status not in (STATUS_IDLE, STATUS_WORKING):
                logger.info("Task %s rejected: session status=%s", task_id, self.status)
                return False

            if len(self._pending_tasks) >= MAX_PENDING_TASKS:
                logger.info("Task %s rejected: pending queue full (%d)",
                            task_id, MAX_PENDING_TASKS)
                return False

        # Validate task origin (master key signature)
        if not self._verify_task_origin(task):
            logger.warning("Task %s rejected: invalid origin signature", task_id)
            return False

        # Check scope compatibility
        if not self._check_scope_match(task):
            logger.info("Task %s rejected: scope mismatch (session=%s)",
                        task_id, self.task_scope)
            return False

        # Privacy filter through shard engine
        filtered_task = self._apply_shard_filter(task)
        if filtered_task is None:
            logger.warning("Task %s rejected: shard filter denied", task_id)
            return False

        # Queue the task
        with self._lock:
            filtered_task['received_at'] = time.time()
            filtered_task['status'] = 'pending'
            self._pending_tasks.append(filtered_task)

        # Also add to instruction queue for dependency ordering
        self._enqueue_instruction(filtered_task)

        logger.info("Task %s accepted (pending=%d)", task_id,
                     len(self._pending_tasks))

        # If the session is idle, immediately start working on the task
        with self._lock:
            is_idle = (self.status == STATUS_IDLE and self.current_task is None)
        if is_idle:
            self._execute_next_task()

        return True

    def _execute_next_task(self):
        """Pick the highest-priority pending task and execute it.

        Called after receive_task() when idle, or by the daemon tick.
        Runs synchronously -- caller should invoke from a background thread
        if non-blocking execution is required.
        """
        with self._lock:
            if self.status not in (STATUS_IDLE,):
                return  # Already working or paused/disconnected
            if not self._pending_tasks:
                return

            # Sort by priority (descending), then by received_at (ascending)
            self._pending_tasks.sort(
                key=lambda t: (-t.get('priority', 0), t.get('received_at', 0))
            )
            task = self._pending_tasks[0]

        task_id = task.get('task_id', 'unknown')
        logger.info("Executing next task: %s", task_id)

        # Execute the task through the full pipeline
        result = self._execute_task_steps(task)

        # Report the result back to the hive
        self._report_result(task_id, result)

    def _execute_task_steps(self, task: Dict) -> Dict:
        """Full task execution pipeline:

        1. Read instructions from task payload / shard
        2. Apply shard filter for privacy
        3. Execute via instruction queue + CREATE/REUSE pipeline
        4. Collect and return results

        Args:
            task: Filtered task dict from the pending queue.

        Returns:
            Result dict compatible with execute_task() output.
        """
        task_id = task.get('task_id', 'unknown')
        start_time = time.time()

        with self._lock:
            self.status = STATUS_WORKING
            self.current_task = task

        result = {
            'task_id': task_id,
            'status': 'pending',
            'changes': [],
            'test_results': None,
            'error': None,
            'duration_s': 0.0,
            'complexity_score': 0,
        }

        try:
            # Step 1: Read instructions (may come from shard or full payload)
            description = task.get('description', '')
            instructions = task.get('instructions', description)
            target_files = task.get('target_files', task.get('files_scope', []))

            # Step 2: If shard was provided, use shard content
            shard = task.get('shard')
            if shard and isinstance(shard, dict):
                # Merge shard interface info into instructions
                shard_content = shard.get('full_content', '')
                if shard_content:
                    instructions = (
                        f"{instructions}\n\n--- Shard context ---\n{shard_content}"
                    )

            # Step 3: Execute via instruction queue + pipeline
            plan = self._build_execution_plan(instructions, target_files)
            execution_result = self._dispatch_to_pipeline(
                instructions, target_files, plan)

            if execution_result.get('error'):
                result['status'] = 'error'
                result['error'] = execution_result['error']
            else:
                result['status'] = 'completed'
                result['changes'] = execution_result.get('changes', [])
                result['test_results'] = execution_result.get('test_results')

            # Step 4: Score complexity
            result['complexity_score'] = self._score_complexity(
                target_files, result['changes'])

        except Exception as e:
            logger.error("Task %s execution steps error: %s", task_id, e)
            result['status'] = 'error'
            result['error'] = str(e)

        result['duration_s'] = round(time.time() - start_time, 2)

        # Restore session state
        with self._lock:
            self.current_task = None
            self.status = STATUS_IDLE

            # Remove from pending queue
            self._pending_tasks = [
                t for t in self._pending_tasks
                if t.get('task_id') != task_id
            ]

        return result

    def _report_result(self, task_id: str, result: Dict):
        """Report task result back to the hive.

        1. Calls report_result() for local stats + PeerLink + EventBus
        2. Notifies HiveTaskDispatcher.on_task_result() for Spark award
        3. Emits EVENT_TASK_COMPLETED
        """
        # Update local stats and publish via PeerLink/EventBus
        self.report_result(task_id, result)

        # Notify the HiveTaskDispatcher for validation + Spark distribution
        try:
            from integrations.coding_agent.hive_task_protocol import get_dispatcher
            dispatcher = get_dispatcher()

            # Build result dict expected by the dispatcher
            dispatcher_result = {
                'files_changed': [c.get('file') for c in result.get('changes', [])],
                'diff': '\n'.join(
                    c.get('diff', '') for c in result.get('changes', [])),
                'tests_passed': (
                    'passed' in (result.get('test_results') or '').lower()
                    if result.get('test_results') else None
                ),
                'test_output': result.get('test_results'),
                'error': result.get('error'),
            }

            reward_info = dispatcher.on_task_result(task_id, dispatcher_result)
            if reward_info.get('spark_awarded', 0) > 0:
                logger.info(
                    "Task %s earned %d Spark (quality=%.3f)",
                    task_id, reward_info['spark_awarded'],
                    reward_info.get('quality_score', 0.0),
                )
        except ImportError:
            logger.debug("HiveTaskDispatcher not available for result reporting")
        except Exception as e:
            logger.debug("Dispatcher result notification failed: %s", e)

    # ─── Task Execution ──────────────────────────────────────────────

    def execute_task(self, task: Dict) -> Dict:
        """Execute a coding task using Claude Code capabilities.

        This is the bridge between hive dispatch and Claude Code's
        native ability to read files, edit code, and run tests.

        Does NOT auto-commit -- returns proposed changes for user approval.

        Args:
            task: Filtered task dict with description, target_files, etc.

        Returns:
            Result dict: {task_id, status, changes, test_results, error,
                          duration_s, complexity_score}
        """
        task_id = task.get('task_id', 'unknown')
        start_time = time.time()

        with self._lock:
            if self.status == STATUS_PAUSED:
                return {
                    'task_id': task_id,
                    'status': 'rejected',
                    'error': 'Session is paused',
                    'changes': [],
                    'test_results': None,
                }

            self.status = STATUS_WORKING
            self.current_task = task

        result = {
            'task_id': task_id,
            'status': 'pending',
            'changes': [],
            'test_results': None,
            'error': None,
            'duration_s': 0.0,
            'complexity_score': 0,
        }

        try:
            # 1. Parse task instructions
            description = task.get('description', '')
            target_files = task.get('target_files', [])
            test_expectations = task.get('test_expectations', [])

            # 2. Build execution plan
            plan = self._build_execution_plan(description, target_files)

            # 3. Dispatch through CREATE/REUSE pipeline
            execution_result = self._dispatch_to_pipeline(
                description, target_files, plan)

            if execution_result.get('error'):
                result['status'] = 'error'
                result['error'] = execution_result['error']
            else:
                result['status'] = 'completed'
                result['changes'] = execution_result.get('changes', [])
                result['test_results'] = execution_result.get('test_results')

            # 4. Score complexity
            result['complexity_score'] = self._score_complexity(
                target_files, result['changes'])

        except Exception as e:
            logger.error("Task %s execution error: %s", task_id, e)
            result['status'] = 'error'
            result['error'] = str(e)

        result['duration_s'] = round(time.time() - start_time, 2)

        # Update session state
        with self._lock:
            self.current_task = None
            self.status = STATUS_IDLE

            # Remove from pending
            self._pending_tasks = [
                t for t in self._pending_tasks
                if t.get('task_id') != task_id
            ]

        return result

    def report_result(self, task_id: str, result: Dict) -> bool:
        """Publish task result back to the hive via PeerLink + EventBus.

        Includes quality metrics and triggers Spark reward calculation.

        Args:
            task_id: The completed task's ID
            result: Output from execute_task()

        Returns:
            True if result was successfully published
        """
        with self._lock:
            session_id = self.session_id
            user_id = self.user_id
            if not session_id:
                return False

        quality_score = self._compute_quality_score(result)
        spark_reward = self._calculate_spark_reward(result, quality_score)

        report = {
            'session_id': session_id,
            'user_id': user_id,
            'task_id': task_id,
            'status': result.get('status', 'unknown'),
            'changes_count': len(result.get('changes', [])),
            'test_results': result.get('test_results'),
            'error': result.get('error'),
            'duration_s': result.get('duration_s', 0),
            'quality_score': quality_score,
            'spark_reward': spark_reward,
            'complexity_score': result.get('complexity_score', 0),
            'reported_at': time.time(),
        }

        # Update local stats
        with self._lock:
            if result.get('status') == 'completed':
                self.stats['tasks_completed'] += 1
                total = self.stats['tasks_completed']
                old_total_q = self.stats['total_quality_points']
                new_total_q = old_total_q + quality_score
                self.stats['total_quality_points'] = new_total_q
                self.stats['avg_quality_score'] = (
                    round(new_total_q / total, 3) if total > 0 else 0.0
                )
            else:
                self.stats['tasks_failed'] += 1
            self.stats['spark_earned'] += spark_reward

            # Add to completed history (capped)
            self._completed_tasks.append(report)
            if len(self._completed_tasks) > self._max_completed_history:
                self._completed_tasks = self._completed_tasks[
                    -self._max_completed_history:]

        # Publish via PeerLink
        published = self._publish_via_peer_link('dispatch', {
            'type': 'task_result',
            'payload': report,
        })

        # Emit EventBus event
        self._emit_event(EVENT_TASK_COMPLETED, report)

        # Record Spark reward
        self._record_spark_reward(user_id, task_id, spark_reward)

        logger.info(
            "Task %s result reported: status=%s quality=%.2f spark=%d",
            task_id, result.get('status'), quality_score, spark_reward,
        )
        return published or True  # Event was emitted even if PeerLink unavailable

    # ─── Status & Configuration ──────────────────────────────────────

    def get_status(self) -> Dict:
        """Return current session state, stats, and active task info."""
        with self._lock:
            return {
                'session_id': self.session_id,
                'user_id': self.user_id,
                'status': self.status,
                'task_scope': self.task_scope,
                'capabilities': self.capabilities,
                'current_task': (
                    {'task_id': self.current_task.get('task_id'),
                     'description': self.current_task.get('description', '')[:200]}
                    if self.current_task else None
                ),
                'pending_tasks': len(self._pending_tasks),
                'completed_tasks': len(self._completed_tasks),
                'stats': dict(self.stats),
                'peer_link_id': self._peer_link_id,
            }

    def set_task_scope(self, scope: str) -> Dict:
        """Update what tasks this session accepts.

        Args:
            scope: 'own_repos', 'public', or 'any'

        Returns:
            Result dict with success flag
        """
        if scope not in TASK_SCOPES:
            return {
                'success': False,
                'error': f'Invalid scope: {scope}. Must be one of {TASK_SCOPES}',
            }
        with self._lock:
            old_scope = self.task_scope
            self.task_scope = scope
        logger.info("Task scope changed: %s -> %s", old_scope, scope)
        return {'success': True, 'previous_scope': old_scope, 'scope': scope}

    def pause(self) -> Dict:
        """Temporarily stop accepting new tasks.

        Current task continues if one is in progress.
        """
        with self._lock:
            if self.status == STATUS_DISCONNECTED:
                return {'success': False, 'error': 'Not connected'}
            if self.status == STATUS_PAUSED:
                return {'success': True, 'message': 'Already paused'}
            prev = self.status
            self.status = STATUS_PAUSED
        logger.info("Hive session paused (was %s)", prev)
        return {'success': True, 'previous_status': prev, 'status': STATUS_PAUSED}

    def resume(self) -> Dict:
        """Resume accepting tasks after a pause."""
        with self._lock:
            if self.status != STATUS_PAUSED:
                return {
                    'success': False,
                    'error': f'Not paused (status={self.status})',
                }
            self.status = STATUS_IDLE
        logger.info("Hive session resumed")
        return {'success': True, 'status': STATUS_IDLE}

    def get_tasks(self) -> Dict:
        """List pending and completed tasks."""
        with self._lock:
            pending = [
                {'task_id': t.get('task_id'), 'description': t.get('description', '')[:200],
                 'received_at': t.get('received_at')}
                for t in self._pending_tasks
            ]
            completed = [
                {'task_id': t.get('task_id'), 'status': t.get('status'),
                 'quality_score': t.get('quality_score'),
                 'spark_reward': t.get('spark_reward'),
                 'reported_at': t.get('reported_at')}
                for t in self._completed_tasks[-50:]  # Last 50
            ]
        return {'pending': pending, 'completed': completed}

    # ─── Internal: PeerLink Integration ──────────────────────────────

    def _register_peer_link(self) -> Optional[str]:
        """Register this session as a CODING_AGENT peer in PeerLinkManager."""
        try:
            from core.peer_link.link_manager import get_link_manager
            manager = get_link_manager()
            if not manager:
                return None

            # Generate a peer ID for this session
            node_id = os.environ.get('HEVOLVE_NODE_ID', '')
            if not node_id:
                node_id = hashlib.sha256(
                    f"{self.user_id}:{self.session_id}".encode()
                ).hexdigest()[:16]

            peer_id = f"{PEER_TYPE}_{node_id}_{self.session_id}"

            # Advertise capabilities so the hive dispatcher can match tasks
            manager.broadcast('dispatch', {
                'type': 'peer_announce',
                'peer_type': PEER_TYPE,
                'peer_id': peer_id,
                'user_id': self.user_id,
                'session_id': self.session_id,
                'capabilities': self.capabilities,
                'task_scope': self.task_scope,
            })

            return peer_id
        except ImportError:
            logger.debug("PeerLink not available, running in local-only mode")
            return None
        except Exception as e:
            logger.warning("PeerLink registration failed: %s", e)
            return None

    def _unregister_peer_link(self):
        """Unregister from PeerLink on disconnect."""
        try:
            from core.peer_link.link_manager import get_link_manager
            manager = get_link_manager()
            if not manager:
                return

            manager.broadcast('dispatch', {
                'type': 'peer_depart',
                'peer_type': PEER_TYPE,
                'peer_id': self._peer_link_id or '',
                'user_id': self.user_id,
                'session_id': self.session_id,
            })
        except Exception:
            pass

    def _publish_via_peer_link(self, channel: str, data: Dict) -> bool:
        """Publish data via PeerLink broadcast."""
        try:
            from core.peer_link.link_manager import get_link_manager
            manager = get_link_manager()
            if manager:
                sent = manager.broadcast(channel, data)
                return sent > 0
        except Exception as e:
            logger.debug("PeerLink publish failed: %s", e)
        return False

    # ─── Internal: EventBus Integration ──────────────────────────────

    def _subscribe_events(self):
        """Subscribe to hive task dispatch events on the platform EventBus."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry or not registry.has('events'):
                return
            bus = registry.get('events')

            def on_task_dispatched(topic, data):
                """Handle incoming task from hive dispatcher."""
                if not isinstance(data, dict):
                    return
                # Only accept tasks targeted at us or broadcast
                target = data.get('target_session')
                if target and target != self.session_id:
                    return
                self.receive_task(data)

            bus.on(EVENT_TASK_DISPATCHED, on_task_dispatched)
            self._event_subscriptions.append(
                (EVENT_TASK_DISPATCHED, on_task_dispatched))
        except Exception as e:
            logger.debug("EventBus subscription failed: %s", e)

    def _unsubscribe_events(self):
        """Unsubscribe all EventBus listeners."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry or not registry.has('events'):
                return
            bus = registry.get('events')
            for topic, callback in self._event_subscriptions:
                bus.off(topic, callback)
        except Exception:
            pass
        self._event_subscriptions.clear()

    def _emit_event(self, topic: str, data: Any):
        """Emit an event on the platform EventBus (best-effort)."""
        try:
            from core.platform.events import emit_event
            emit_event(topic, data)
        except Exception:
            pass

    # ─── Internal: Instruction Queue ─────────────────────────────────

    def _init_instruction_queue(self):
        """Initialize local instruction queue for incoming tasks."""
        try:
            from integrations.agent_engine.instruction_queue import get_queue
            self._instruction_queue = get_queue(self.user_id)
        except Exception as e:
            logger.debug("Instruction queue init failed: %s", e)
            self._instruction_queue = None

    def _enqueue_instruction(self, task: Dict):
        """Add a task to the instruction queue for dependency ordering."""
        try:
            from integrations.agent_engine.instruction_queue import enqueue_instruction
            enqueue_instruction(
                user_id=self.user_id,
                text=task.get('description', '')[:2000],
                priority=task.get('priority', 5),
                tags=['hive_task', PEER_TYPE],
                context={
                    'task_id': task.get('task_id'),
                    'source': 'hive_session',
                    'session_id': self.session_id,
                    'target_files': task.get('target_files', []),
                },
            )
        except Exception as e:
            logger.debug("Instruction enqueue failed: %s", e)

    # ─── Internal: Security ──────────────────────────────────────────

    def _verify_task_origin(self, task: Dict) -> bool:
        """Verify that a task was dispatched by a legitimate hive node.

        Uses master key signature verification to prevent rogue dispatchers.
        Tasks without a signature are accepted only from SAME_USER trust level.
        """
        signature = task.get('origin_signature', '')
        if not signature:
            # Allow unsigned tasks only from SAME_USER trust (own devices)
            trust = task.get('trust_level', '')
            if trust == 'same_user':
                return True
            logger.debug("Task %s has no signature and trust=%s",
                         task.get('task_id'), trust)
            return False

        try:
            from security.master_key import verify_master_signature
            # Build payload to verify (exclude the signature field itself)
            payload = {k: v for k, v in task.items()
                       if k != 'origin_signature'}
            return verify_master_signature(payload, signature)
        except ImportError:
            logger.warning("master_key module unavailable — rejecting task (cannot verify)")
            return False
        except Exception as e:
            logger.warning("Task origin verification failed: %s", e)
            return False

    def _check_scope_match(self, task: Dict) -> bool:
        """Check whether a task matches this session's scope setting."""
        task_scope_level = task.get('scope_level', 'any')

        if self.task_scope == 'any':
            return True
        if self.task_scope == 'public':
            return task_scope_level in ('public', 'own_repos')
        if self.task_scope == 'own_repos':
            # Only accept tasks for repos owned by this user
            task_owner = task.get('repo_owner', '')
            return task_owner == self.user_id or task_scope_level == 'own_repos'
        return False

    def _apply_shard_filter(self, task: Dict) -> Optional[Dict]:
        """Filter task content through the shard engine based on trust level.

        - SAME_USER trust: FULL_FILE scope (see everything)
        - PEER trust: INTERFACES scope (signatures + types only)
        - RELAY/unknown: SIGNATURES scope (minimal)
        """
        trust = task.get('trust_level', 'relay')

        try:
            from integrations.agent_engine.shard_engine import (
                ShardScope, ShardEngine, get_shard_engine,
            )

            if trust == 'same_user':
                scope = ShardScope.FULL_FILE
            elif trust == 'peer':
                scope = ShardScope.INTERFACES
            else:
                scope = ShardScope.SIGNATURES

            # If task has raw file content and we need to filter it down,
            # run it through the shard engine
            if task.get('full_content') and scope != ShardScope.FULL_FILE:
                engine = get_shard_engine()
                shard = engine.create_shard(
                    task=task.get('description', ''),
                    target_files=task.get('target_files', []),
                    scope=scope,
                )
                # Replace full content with filtered view
                filtered = dict(task)
                filtered['full_content'] = shard.full_content
                filtered['interface_specs'] = [
                    s.file_path for s in shard.interface_specs
                ]
                filtered['shard_scope'] = scope.value
                return filtered

        except ImportError:
            logger.debug("Shard engine not available, passing task as-is")
        except Exception as e:
            logger.warning("Shard filter error: %s", e)

        # Pass through unmodified (no shard engine or no filtering needed)
        return dict(task)

    # ─── Internal: Task Execution ────────────────────────────────────

    def _build_execution_plan(self, description: str,
                              target_files: List[str]) -> Dict:
        """Build an execution plan from task description.

        Returns a structured plan dict that the pipeline can execute.
        """
        plan = {
            'description': description,
            'steps': [],
        }

        # Determine steps from description
        if target_files:
            plan['steps'].append({
                'action': 'read_files',
                'files': target_files,
            })

        plan['steps'].append({
            'action': 'execute_instructions',
            'description': description,
        })

        if target_files:
            plan['steps'].append({
                'action': 'run_tests',
                'scope': 'affected',
            })

        return plan

    def _dispatch_to_pipeline(self, description: str,
                              target_files: List[str],
                              plan: Dict) -> Dict:
        """Dispatch the task through the CREATE/REUSE pipeline.

        Uses dispatch_goal() (3-tier: in-process, HTTP, llama.cpp fallback)
        to execute the coding task through the standard agent pipeline.
        """
        result = {
            'changes': [],
            'test_results': None,
            'error': None,
        }

        prompt = self._build_task_prompt(description, target_files)

        try:
            from integrations.agent_engine.dispatch import dispatch_goal

            # Generate a deterministic goal_id from task content
            goal_id = hashlib.sha256(
                f"hive:{self.session_id}:{description[:200]}".encode()
            ).hexdigest()[:16]

            response = dispatch_goal(
                prompt=prompt,
                user_id=self.user_id,
                goal_id=goal_id,
                goal_type='coding',
            )

            if response:
                result['changes'] = self._parse_changes_from_response(response)
                result['test_results'] = self._extract_test_results(response)
            else:
                result['error'] = 'Pipeline returned no response'

        except Exception as e:
            result['error'] = f'Pipeline dispatch failed: {e}'

        return result

    def _build_task_prompt(self, description: str,
                           target_files: List[str]) -> str:
        """Build a structured prompt for the CREATE/REUSE pipeline."""
        parts = [
            f"HIVE CODING TASK (session {self.session_id}):",
            f"\n{description}",
        ]
        if target_files:
            parts.append(f"\nTarget files: {', '.join(target_files)}")
        parts.append("\nProvide changes as file diffs. Do NOT commit.")
        return '\n'.join(parts)

    def _parse_changes_from_response(self, response: str) -> List[Dict]:
        """Extract file changes from pipeline response."""
        changes = []
        # Look for diff-like patterns in the response
        current_file = None
        current_diff_lines = []

        for line in response.split('\n'):
            if line.startswith('--- a/') or line.startswith('+++ b/'):
                fname = line[6:].strip()
                if line.startswith('+++ b/') and fname:
                    current_file = fname
            elif line.startswith('@@') and current_file:
                if current_diff_lines and current_file:
                    changes.append({
                        'file': current_file,
                        'diff': '\n'.join(current_diff_lines),
                    })
                current_diff_lines = [line]
            elif current_file and (line.startswith('+') or line.startswith('-')
                                   or line.startswith(' ')):
                current_diff_lines.append(line)

        # Flush last change
        if current_file and current_diff_lines:
            changes.append({
                'file': current_file,
                'diff': '\n'.join(current_diff_lines),
            })

        return changes

    def _extract_test_results(self, response: str) -> Optional[str]:
        """Extract test results from pipeline response, if any."""
        # Look for common test output patterns
        markers = ['PASSED', 'FAILED', 'ERROR', 'tests passed',
                    'test session', 'pytest']
        for marker in markers:
            idx = response.lower().find(marker.lower())
            if idx >= 0:
                # Return surrounding context
                start = max(0, idx - 200)
                end = min(len(response), idx + 500)
                return response[start:end]
        return None

    # ─── Internal: Quality & Rewards ─────────────────────────────────

    def _compute_quality_score(self, result: Dict) -> float:
        """Compute a quality score for a completed task (0.0 to 1.0)."""
        if result.get('status') != 'completed':
            return 0.0

        score = 0.5  # Base score for completion

        # Bonus for having changes
        changes = result.get('changes', [])
        if changes:
            score += 0.2

        # Bonus for passing tests
        test_results = result.get('test_results', '')
        if test_results:
            lower = test_results.lower()
            if 'passed' in lower and 'failed' not in lower:
                score += 0.3
            elif 'passed' in lower:
                score += 0.1

        return min(1.0, round(score, 3))

    def _calculate_spark_reward(self, result: Dict,
                                quality_score: float) -> int:
        """Calculate Spark token reward for a completed task."""
        if result.get('status') != 'completed':
            return 0

        complexity = result.get('complexity_score', 1)
        base = SPARK_BASE_REWARD
        reward = base + (complexity * SPARK_COMPLEXITY_MULTIPLIER)

        # Quality bonus
        if quality_score >= SPARK_QUALITY_BONUS_THRESHOLD:
            reward = int(reward * 1.5)

        return int(reward)

    def _score_complexity(self, target_files: List[str],
                          changes: List[Dict]) -> int:
        """Score task complexity (1-10) based on files and changes."""
        score = 1
        score += min(len(target_files), 3)  # Up to +3 for multi-file
        score += min(len(changes), 3)       # Up to +3 for many changes

        # Count total diff lines
        total_lines = sum(
            len(c.get('diff', '').split('\n')) for c in changes
        )
        if total_lines > 100:
            score += 2
        elif total_lines > 30:
            score += 1

        return min(10, score)

    def _record_spark_reward(self, user_id: str, task_id: str,
                             spark_amount: int):
        """Record Spark reward in the hosting reward system."""
        if spark_amount <= 0:
            return
        try:
            from integrations.agent_engine.hosting_reward_service import (
                get_hosting_reward_service,
            )
            svc = get_hosting_reward_service()
            if svc:
                svc.record_contribution(
                    user_id=user_id,
                    contribution_type='hive_coding_task',
                    amount=spark_amount,
                    metadata={'task_id': task_id, 'source': 'claude_hive_session'},
                )
        except Exception as e:
            logger.debug("Spark reward recording failed: %s", e)

    # ─── Internal: Flush on Disconnect ───────────────────────────────

    def _flush_pending_tasks(self):
        """Return pending tasks to the hive queue on disconnect."""
        with self._lock:
            tasks = list(self._pending_tasks)
            self._pending_tasks.clear()

        for task in tasks:
            try:
                self._publish_via_peer_link('dispatch', {
                    'type': 'task_returned',
                    'task_id': task.get('task_id'),
                    'reason': 'session_disconnect',
                    'session_id': self.session_id,
                })
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════
# Module-level Singleton
# ═════════════════════════════════════════════════════════════════════

_session: Optional[ClaudeHiveSession] = None
_session_lock = threading.Lock()


def get_hive_session() -> ClaudeHiveSession:
    """Get or create the ClaudeHiveSession singleton."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = ClaudeHiveSession()
    return _session


class SessionRegistry:
    """Registry of all active Claude Code hive sessions.

    The HiveTaskDispatcher.match_session() queries this to find
    available sessions for task assignment. Tracks both the local
    singleton session and any remote sessions announced via PeerLink.

    Thread-safe: all mutations are guarded by ``_lock``.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # session_id -> ClaudeHiveSession or Dict (remote announcement)
        self._sessions: Dict[str, Any] = {}

    def register(self, session) -> None:
        """Register a session (local ClaudeHiveSession or remote announcement dict).

        Args:
            session: A ClaudeHiveSession instance or a dict with at minimum
                     'session_id' and 'status' keys.
        """
        sid = (session.session_id if hasattr(session, 'session_id')
               else session.get('session_id', ''))
        if not sid:
            return
        with self._lock:
            self._sessions[sid] = session
        logger.debug("Session registered in registry: %s", sid)

    def unregister(self, session_id: str) -> None:
        """Remove a session from the registry.

        Args:
            session_id: The session ID to remove.
        """
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed:
            logger.debug("Session unregistered from registry: %s", session_id)

    def get_session(self, session_id: str):
        """Get a specific session by ID.

        Returns:
            ClaudeHiveSession instance or None.
        """
        with self._lock:
            entry = self._sessions.get(session_id)

        # If it's a dict (remote announcement), return None
        # (only real session objects can execute tasks)
        if entry and hasattr(entry, 'receive_task'):
            return entry

        # Fallback: check the module-level singleton
        session = get_hive_session()
        if session.session_id == session_id:
            return session
        return None

    def get_available_sessions(self) -> List[Dict]:
        """Return list of session info dicts available for task assignment.

        Each dict contains the keys expected by HiveTaskDispatcher.match_session():
          - session_id, status, languages, quality_score, region, capabilities

        Returns:
            List of session info dicts.
        """
        results: List[Dict] = []

        # Always include the local singleton if it's active
        local = get_hive_session()
        if local.session_id and local.status in (STATUS_IDLE, STATUS_WORKING):
            results.append(self._session_to_dict(local))

        # Include registered remote sessions
        with self._lock:
            for sid, entry in self._sessions.items():
                if hasattr(entry, 'session_id'):
                    # Local ClaudeHiveSession instance
                    if entry.session_id == local.session_id:
                        continue  # Already included above
                    if entry.status in (STATUS_IDLE, STATUS_WORKING):
                        results.append(self._session_to_dict(entry))
                elif isinstance(entry, dict):
                    # Remote announcement dict
                    status = entry.get('status', '')
                    if status in (STATUS_IDLE, STATUS_WORKING):
                        results.append(entry)

        return results

    @staticmethod
    def _session_to_dict(session) -> Dict:
        """Convert a ClaudeHiveSession to the dict format expected by the dispatcher."""
        caps = getattr(session, 'capabilities', {}) or {}
        stats = getattr(session, 'stats', {}) or {}
        return {
            'session_id': session.session_id,
            'user_id': getattr(session, 'user_id', ''),
            'status': session.status,
            'languages': caps.get('languages', []),
            'frameworks': caps.get('frameworks', []),
            'capabilities': caps,
            'task_scope': getattr(session, 'task_scope', 'own_repos'),
            'quality_score': stats.get('avg_quality_score', 0.5),
            'region': os.environ.get('HEVOLVE_REGION', ''),
            'peer_link_id': getattr(session, '_peer_link_id', ''),
        }


_registry = None
_registry_lock = threading.Lock()


def get_session_registry() -> SessionRegistry:
    """Get or create the SessionRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SessionRegistry()
    return _registry


# ═════════════════════════════════════════════════════════════════════
# Flask Blueprint
# ═════════════════════════════════════════════════════════════════════

_blueprint: Optional[Any] = None
_blueprint_lock = threading.Lock()


def _create_blueprint():
    """Create the hive session Flask Blueprint."""
    try:
        from flask import Blueprint, request, jsonify
    except ImportError:
        return None

    bp = Blueprint('hive_session', __name__)

    @bp.route('/api/hive/session/connect', methods=['POST'])
    def api_connect():
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id', '')
        if not user_id:
            return jsonify({'success': False, 'error': 'user_id is required'}), 400

        capabilities = data.get('capabilities')
        task_scope = data.get('task_scope', 'own_repos')

        session = get_hive_session()
        result = session.connect(
            user_id=user_id,
            capabilities=capabilities,
            task_scope=task_scope,
        )

        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @bp.route('/api/hive/session/disconnect', methods=['POST'])
    def api_disconnect():
        session = get_hive_session()
        result = session.disconnect()
        return jsonify(result), 200

    @bp.route('/api/hive/session/status', methods=['GET'])
    def api_status():
        session = get_hive_session()
        return jsonify(session.get_status()), 200

    @bp.route('/api/hive/session/pause', methods=['POST'])
    def api_pause():
        session = get_hive_session()
        result = session.pause()
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @bp.route('/api/hive/session/resume', methods=['POST'])
    def api_resume():
        session = get_hive_session()
        result = session.resume()
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @bp.route('/api/hive/session/scope', methods=['POST'])
    def api_set_scope():
        data = request.get_json(silent=True) or {}
        scope = data.get('scope', '')
        if not scope:
            return jsonify({'success': False, 'error': 'scope is required'}), 400

        session = get_hive_session()
        result = session.set_task_scope(scope)
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code

    @bp.route('/api/hive/session/tasks', methods=['GET'])
    def api_tasks():
        session = get_hive_session()
        return jsonify(session.get_tasks()), 200

    @bp.route('/api/hive/session/task/<task_id>/result', methods=['POST'])
    def api_task_result(task_id):
        """Report a task result externally (e.g., from a remote session).

        Body JSON: {status, changes, test_results, error, duration_s, ...}
        """
        data = request.get_json(silent=True) or {}
        if not task_id:
            return jsonify({'success': False, 'error': 'task_id is required'}), 400

        session = get_hive_session()
        if session.status == STATUS_DISCONNECTED:
            return jsonify({'success': False, 'error': 'Session not connected'}), 400

        # Build result dict from request body
        result = {
            'task_id': task_id,
            'status': data.get('status', 'completed'),
            'changes': data.get('changes', []),
            'test_results': data.get('test_results'),
            'error': data.get('error'),
            'duration_s': data.get('duration_s', 0.0),
            'complexity_score': data.get('complexity_score', 0),
        }

        published = session.report_result(task_id, result)

        # Also notify the dispatcher if available
        reward_info = {}
        try:
            from integrations.coding_agent.hive_task_protocol import get_dispatcher
            dispatcher = get_dispatcher()
            dispatcher_result = {
                'files_changed': [c.get('file') for c in result.get('changes', [])],
                'diff': '\n'.join(
                    c.get('diff', '') for c in result.get('changes', [])),
                'tests_passed': data.get('tests_passed'),
                'test_output': data.get('test_results'),
                'error': data.get('error'),
            }
            reward_info = dispatcher.on_task_result(task_id, dispatcher_result)
        except Exception:
            pass

        return jsonify({
            'success': True,
            'task_id': task_id,
            'published': published,
            'reward': reward_info,
        }), 200

    return bp


def get_blueprint():
    """Get or create the hive_session Flask Blueprint.

    Returns None if Flask is not installed.
    """
    global _blueprint
    if _blueprint is None:
        with _blueprint_lock:
            if _blueprint is None:
                _blueprint = _create_blueprint()
    return _blueprint


# Public alias matching the naming convention in hive_signal_bridge.py
create_hive_session_blueprint = _create_blueprint


# Convenience alias for registration in hart_intelligence_entry.py:
#   from integrations.coding_agent.claude_hive_session import hive_session_bp
#   if hive_session_bp: app.register_blueprint(hive_session_bp)
hive_session_bp = get_blueprint()
