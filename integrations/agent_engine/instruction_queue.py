"""
Instruction Queue — Never miss a user instruction.

Every user instruction is immediately queued and registered with SmartLedger
for LLM-based dependency analysis.  When compute becomes available,
instructions are pulled in proper dependency order (not just priority),
correlated with existing context, and executed — individually or as a batch.

SmartLedger integration:
  enqueue()        → ledger.add_dynamic_task()   (LLM classifies deps)
  pull_batch()     → ledger.get_next/parallel()  (respects dep graph)
  complete_batch() → ledger.complete_task_and_route() (unblocks dependents)

Falls back to simple priority ordering when SmartLedger is unavailable
(e.g. no LLM reachable, agent_ledger package not installed).

Batch mode: If multiple instructions queue up before compute arrives,
they are consolidated into a single prompt that fits the context window.
Related instructions are grouped, duplicates deduplicated, dependencies
ordered by the ledger's task graph.

Architecture:
  User says "do X" → enqueue_instruction(user_id, "do X")
       │
       ├─ Stored in agent_data/instructions/{user_id}_queue.json
       ├─ Registered with SmartLedger via add_dynamic_task():
       │   ├─ LLM classifies relationship to existing tasks
       │   ├─ Sets prerequisites, blockers, execution mode
       │   └─ Determines parallel vs sequential ordering
       ├─ Correlated with:
       │   ├─ Conversation state (recent messages)
       │   ├─ Related queued instructions (semantic grouping)
       │   ├─ Active goals (avoid duplication)
       │   └─ Ledger tasks (dependency tracking)
       │
       └─ When compute arrives:
            ├─ pull_batch(user_id, max_tokens=...)
            │   └─ Uses ledger.get_next_executable_task() for dep-aware ordering
            ├─ Execute via /chat or dispatch.py
            └─ complete_batch() → ledger.complete_task_and_route()

Integration points:
  - langchain_gpt_api.py /chat endpoint: auto-enqueue if compute busy
  - hart_cli.py: `hart -p "task"` enqueues if server busy
  - dispatch.py: pull_batch when idle compute detected
  - agent_daemon.py: periodic batch check on tick
"""

import json
import logging
import os
import threading
import time
import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.instruction_queue')

# SmartLedger — optional, graceful fallback when unavailable
try:
    from agent_ledger import SmartLedger
    _LEDGER_AVAILABLE = True
except ImportError:
    SmartLedger = None
    _LEDGER_AVAILABLE = False

_QUEUE_DIR = os.path.join(
    os.environ.get('HART_INSTALL_DIR',
                   os.path.dirname(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))))),
    'agent_data', 'instructions',
)


class InstructionStatus(str, Enum):
    QUEUED = 'queued'           # Waiting for compute
    BATCHED = 'batched'         # Included in a batch, not yet executed
    IN_PROGRESS = 'in_progress' # Currently being executed
    DONE = 'done'               # Successfully completed
    FAILED = 'failed'           # Execution failed
    CANCELLED = 'cancelled'     # User cancelled


class Instruction:
    """A single user instruction with metadata."""

    __slots__ = (
        'id', 'user_id', 'text', 'status', 'created_at',
        'updated_at', 'priority', 'tags', 'context',
        'related_goal_id', 'batch_id', 'result', 'error',
    )

    def __init__(self, user_id: str, text: str, priority: int = 5,
                 tags: Optional[List[str]] = None,
                 context: Optional[Dict] = None,
                 related_goal_id: Optional[str] = None):
        self.id = hashlib.sha256(
            f"{user_id}:{text}:{time.time()}".encode()
        ).hexdigest()[:16]
        self.user_id = user_id
        self.text = text
        self.status = InstructionStatus.QUEUED
        self.created_at = datetime.utcnow().isoformat()
        self.updated_at = self.created_at
        self.priority = priority  # 1=highest, 10=lowest
        self.tags = tags or []
        self.context = context or {}
        self.related_goal_id = related_goal_id
        self.batch_id = None
        self.result = None
        self.error = None

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'user_id': self.user_id,
            'text': self.text,
            'status': self.status.value if isinstance(self.status, InstructionStatus) else self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'priority': self.priority,
            'tags': self.tags,
            'context': self.context,
            'related_goal_id': self.related_goal_id,
            'batch_id': self.batch_id,
            'result': self.result,
            'error': self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> 'Instruction':
        inst = cls.__new__(cls)
        inst.id = d['id']
        inst.user_id = d['user_id']
        inst.text = d['text']
        inst.status = InstructionStatus(d.get('status', 'queued'))
        inst.created_at = d.get('created_at', '')
        inst.updated_at = d.get('updated_at', '')
        inst.priority = d.get('priority', 5)
        inst.tags = d.get('tags', [])
        inst.context = d.get('context', {})
        inst.related_goal_id = d.get('related_goal_id')
        inst.batch_id = d.get('batch_id')
        inst.result = d.get('result')
        inst.error = d.get('error')
        return inst


class InstructionBatch:
    """A consolidated batch of related instructions."""

    def __init__(self, batch_id: str, instructions: List[Instruction],
                 consolidated_prompt: str):
        self.batch_id = batch_id
        self.instructions = instructions
        self.consolidated_prompt = consolidated_prompt
        self.created_at = datetime.utcnow().isoformat()
        self.token_estimate = self._estimate_tokens(consolidated_prompt)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate (~4 chars per token for English)."""
        return len(text) // 4

    def to_dict(self) -> Dict:
        return {
            'batch_id': self.batch_id,
            'instruction_ids': [i.id for i in self.instructions],
            'consolidated_prompt': self.consolidated_prompt,
            'created_at': self.created_at,
            'token_estimate': self.token_estimate,
            'instruction_count': len(self.instructions),
        }


class ExecutionPlan:
    """Dependency-aware execution plan for queued instructions.

    Splits instructions into:
    - parallel_groups: list of groups that can run concurrently within each group
    - sequential_chain: ordered list where each depends on the previous

    Dispatcher executes all items in a parallel group concurrently,
    waits for completion, then moves to the next group.

    Example for instructions: [A, B, C, D, E]
      If A,B are independent and C depends on A, D depends on B, E depends on C+D:
        waves = [[A, B], [C, D], [E]]
      Wave 1: dispatch A and B in parallel
      Wave 2: after A+B complete, dispatch C and D in parallel
      Wave 3: after C+D complete, dispatch E
    """

    def __init__(self, waves: List[List[Instruction]], batch_id: str):
        self.waves = waves  # List of parallel groups in dependency order
        self.batch_id = batch_id
        self.created_at = datetime.utcnow().isoformat()
        self.total_instructions = sum(len(w) for w in waves)

    def to_dict(self) -> Dict:
        return {
            'batch_id': self.batch_id,
            'waves': [
                [{'id': i.id, 'text': i.text[:100]} for i in wave]
                for wave in self.waves
            ],
            'total_instructions': self.total_instructions,
            'wave_count': len(self.waves),
        }


class InstructionQueue:
    """Persistent instruction queue per user.

    Thread-safe. File-backed (JSON). Survives restarts.
    Uses SmartLedger for LLM-based dependency analysis between instructions.

    Concurrency guarantees:
    - _lock (RLock): serializes ALL state mutations (instructions, ledger, file I/O)
    - _drain_lock (Lock): prevents concurrent drains for same user
    - _save() uses atomic write (temp + rename) to prevent corruption
    - SmartLedger is always accessed inside _lock — no separate ledger lock needed
    - Cross-process safety: file lock via _QUEUE_DIR/{user}_drain.lock
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._lock = threading.RLock()       # Protects all state mutations
        self._drain_lock = threading.Lock()  # Prevents concurrent drains
        self._queue_path = os.path.join(_QUEUE_DIR, f'{user_id}_queue.json')
        self._drain_lock_path = os.path.join(_QUEUE_DIR, f'{user_id}_drain.lock')
        self._instructions: Dict[str, Instruction] = {}
        self._ledger = None  # Lazy-initialized SmartLedger
        self._task_map: Dict[str, str] = {}  # instruction_id → ledger_task_id
        self._load()

    def acquire_drain_lock(self, timeout: float = 0) -> bool:
        """Acquire exclusive drain lock for this user's queue.

        Prevents concurrent drains (daemon tick + API call + another agent).
        Uses both an in-process threading.Lock AND a filesystem lock file
        for cross-process safety (daemon vs. API server in separate processes).

        Args:
            timeout: Seconds to wait. 0 = non-blocking (return False immediately if busy).

        Returns:
            True if lock acquired, False if another drain is in progress.
        """
        acquired = self._drain_lock.acquire(timeout=timeout) if timeout > 0 else self._drain_lock.acquire(blocking=False)
        if not acquired:
            return False

        # Cross-process file lock: write PID to lock file
        try:
            os.makedirs(os.path.dirname(self._drain_lock_path), exist_ok=True)
            if os.path.exists(self._drain_lock_path):
                try:
                    with open(self._drain_lock_path, 'r') as f:
                        lock_data = json.load(f)
                    lock_pid = lock_data.get('pid', -1)
                    lock_time = lock_data.get('time', 0)
                    # Check if lock holder is still alive
                    if lock_pid != os.getpid():
                        try:
                            os.kill(lock_pid, 0)  # Check if process exists
                            # Process alive — check staleness (10 min max)
                            if time.time() - lock_time < 600:
                                self._drain_lock.release()
                                return False
                            # Stale lock — take it
                        except (OSError, ProcessLookupError):
                            pass  # Dead process — take the lock
                except (json.JSONDecodeError, IOError):
                    pass  # Corrupt lock file — take it

            with open(self._drain_lock_path, 'w') as f:
                json.dump({'pid': os.getpid(), 'time': time.time(),
                           'user_id': self.user_id}, f)
        except IOError:
            pass  # File lock is best-effort — thread lock still held

        return True

    def release_drain_lock(self):
        """Release the drain lock."""
        try:
            if os.path.exists(self._drain_lock_path):
                os.remove(self._drain_lock_path)
        except OSError:
            pass
        try:
            self._drain_lock.release()
        except RuntimeError:
            pass  # Already released

    def _get_ledger(self):
        """Get or create SmartLedger for this user's instruction queue.

        The ledger provides LLM-based dependency analysis between queued
        instructions via add_dynamic_task().  Returns None when SmartLedger
        is unavailable — caller must fall back to simple priority ordering.
        """
        if self._ledger is not None:
            return self._ledger
        if not _LEDGER_AVAILABLE:
            return None
        try:
            # Dedicated ledger for the instruction queue — separate from
            # per-prompt action ledgers in create_recipe.py.
            # agent_id = 'iq_{user}' distinguishes this from action ledgers.
            self._ledger = SmartLedger(
                agent_id=f'iq_{self.user_id}',
                session_id=f'{self.user_id}_instruction_queue',
            )
            logger.info(f"SmartLedger initialized for instruction queue [{self.user_id}]")
            return self._ledger
        except Exception as e:
            logger.debug(f"SmartLedger unavailable for instruction queue: {e}")
            return None

    def _register_with_ledger(self, inst: 'Instruction') -> Optional[str]:
        """Register an instruction with SmartLedger for dependency analysis.

        Calls ledger.add_dynamic_task() which uses LLM to classify the
        relationship between this instruction and all existing tasks:
        - child/sibling/sequential/conditional/independent
        - prerequisites, blockers, execution mode (parallel/sequential)
        - delegation needs, scheduling, retry config

        Returns the ledger task_id on success, None on failure.
        """
        ledger = self._get_ledger()
        if ledger is None:
            return None

        try:
            task = ledger.add_dynamic_task(
                task_description=inst.text,
                context={
                    'current_action_id': None,
                    'previous_outcome': None,
                    'user_message': inst.text,
                    'discovered_by': 'instruction_queue',
                    'instruction_id': inst.id,
                    'priority': inst.priority,
                    'tags': inst.tags,
                    'related_goal_id': inst.related_goal_id,
                },
            )
            if task:
                task_id = task.task_id
                self._task_map[inst.id] = task_id
                inst.context['ledger_task_id'] = task_id
                logger.info(
                    f"Instruction [{inst.id}] registered with ledger as [{task_id}]"
                )
                return task_id
        except Exception as e:
            logger.debug(f"Ledger registration failed for [{inst.id}]: {e}")
        return None

    def _load(self):
        """Load queue from disk. Auto-detects encrypted vs plaintext."""
        if not os.path.exists(self._queue_path):
            return
        try:
            try:
                from security.crypto import decrypt_json_file
                data = decrypt_json_file(self._queue_path)
            except ImportError:
                with open(self._queue_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            if data is None:
                return
            for d in data.get('instructions', []):
                inst = Instruction.from_dict(d)
                self._instructions[inst.id] = inst
                # Restore task map from persisted context
                ltid = inst.context.get('ledger_task_id')
                if ltid:
                    self._task_map[inst.id] = ltid
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.warning(f"Failed to load instruction queue: {e}")

    def _save(self):
        """Persist queue to disk using atomic write (temp + rename).

        Encrypted at rest when HEVOLVE_DATA_KEY is configured.
        Prevents corruption if process crashes mid-write: the rename is
        atomic on POSIX and near-atomic on Windows (NTFS MoveFileEx).
        """
        os.makedirs(os.path.dirname(self._queue_path), exist_ok=True)
        data = {
            'user_id': self.user_id,
            'updated_at': datetime.utcnow().isoformat(),
            'instructions': [i.to_dict() for i in self._instructions.values()],
        }
        tmp_path = self._queue_path + '.tmp'
        try:
            try:
                from security.crypto import encrypt_json_file
                encrypt_json_file(tmp_path, data)
            except ImportError:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
            # Atomic rename (replaces target on POSIX; os.replace on all platforms)
            os.replace(tmp_path, self._queue_path)
        except IOError as e:
            logger.error(f"Failed to save instruction queue: {e}")
            # Clean up temp file on failure
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def enqueue(self, text: str, priority: int = 5,
                tags: Optional[List[str]] = None,
                context: Optional[Dict] = None,
                related_goal_id: Optional[str] = None) -> Instruction:
        """Add an instruction to the queue.

        The instruction is persisted to JSON AND registered with SmartLedger
        for LLM-based dependency analysis.  The ledger classifies how this
        instruction relates to all other queued instructions (child, sibling,
        sequential, conditional, independent) and sets prerequisites/blockers.

        When SmartLedger is unavailable, falls back to simple priority ordering.
        """
        with self._lock:
            # Deduplicate: if exact same text is already queued, skip
            for inst in self._instructions.values():
                if inst.text == text and inst.status == InstructionStatus.QUEUED:
                    logger.debug(f"Duplicate instruction skipped: {text[:50]}...")
                    return inst

            inst = Instruction(
                user_id=self.user_id,
                text=text,
                priority=priority,
                tags=tags,
                context=context,
                related_goal_id=related_goal_id,
            )
            self._instructions[inst.id] = inst

            # Register with SmartLedger for LLM dependency analysis.
            # This call may invoke the LLM to classify task relationships.
            self._register_with_ledger(inst)

            self._save()
            logger.info(f"Instruction queued [{inst.id}]: {text[:80]}...")
            return inst

    def get_pending(self) -> List[Instruction]:
        """Get all queued (unconsumed) instructions, sorted by priority then time."""
        with self._lock:
            pending = [
                i for i in self._instructions.values()
                if i.status == InstructionStatus.QUEUED
            ]
            pending.sort(key=lambda i: (i.priority, i.created_at))
            return pending

    def get_all(self) -> List[Instruction]:
        """Get all instructions regardless of status."""
        with self._lock:
            return list(self._instructions.values())

    def mark_status(self, instruction_id: str, status: InstructionStatus,
                    result: Optional[str] = None,
                    error: Optional[str] = None):
        """Update instruction status."""
        with self._lock:
            inst = self._instructions.get(instruction_id)
            if inst:
                inst.status = status
                inst.updated_at = datetime.utcnow().isoformat()
                if result:
                    inst.result = result
                if error:
                    inst.error = error
                self._save()

    def cancel(self, instruction_id: str):
        """Cancel a queued instruction."""
        self.mark_status(instruction_id, InstructionStatus.CANCELLED)

    def _get_ledger_ordered_pending(self) -> List['Instruction']:
        """Get pending instructions ordered by SmartLedger dependency graph.

        Uses get_next_executable_task() and get_parallel_executable_tasks()
        to build a dependency-aware execution order.  Tasks whose
        prerequisites aren't met yet are excluded (they'll be pulled in
        a future batch after their dependencies complete).

        Returns simple priority-sorted list when ledger is unavailable.
        """
        pending = self.get_pending()
        if not pending:
            return []

        ledger = self._get_ledger()
        if ledger is None or not self._task_map:
            return pending  # Fallback: simple priority order

        # Build reverse map: ledger_task_id → Instruction
        task_to_inst: Dict[str, 'Instruction'] = {}
        unmapped: List['Instruction'] = []
        for inst in pending:
            ltid = self._task_map.get(inst.id)
            if ltid and ltid in ledger.tasks:
                task_to_inst[ltid] = inst
            else:
                unmapped.append(inst)

        if not task_to_inst:
            return pending  # No mapped tasks, use simple ordering

        # Collect executable tasks from ledger in dependency order
        ordered: List['Instruction'] = []
        seen: set = set()

        # First: all parallel-ready tasks (can run concurrently)
        try:
            parallel = ledger.get_parallel_executable_tasks()
            for task in parallel:
                if task.task_id in task_to_inst and task.task_id not in seen:
                    ordered.append(task_to_inst[task.task_id])
                    seen.add(task.task_id)
        except Exception as e:
            logger.debug(f"Ledger parallel query failed: {e}")

        # Then: sequential tasks in dependency order
        try:
            max_iter = len(task_to_inst) + 5  # safety bound
            for _ in range(max_iter):
                task = ledger.get_next_executable_task()
                if task is None:
                    break
                if task.task_id in task_to_inst and task.task_id not in seen:
                    ordered.append(task_to_inst[task.task_id])
                    seen.add(task.task_id)
                elif task.task_id in seen:
                    # Already included, skip to avoid infinite loop
                    break
                else:
                    break  # Not an instruction queue task
        except Exception as e:
            logger.debug(f"Ledger sequential query failed: {e}")

        # Append any mapped but not-yet-executable instructions at the end
        # (their deps aren't met — they'll wait for next batch)
        for ltid, inst in task_to_inst.items():
            if ltid not in seen:
                ordered.append(inst)

        # Append unmapped instructions last
        ordered.extend(unmapped)

        logger.info(
            f"Ledger-ordered batch: {len(ordered)} instructions "
            f"({len(seen)} dependency-resolved, {len(unmapped)} unmapped)"
        )
        return ordered

    def pull_batch(self, max_tokens: int = 8000,
                   max_instructions: int = 20) -> Optional[InstructionBatch]:
        """Pull queued instructions into a consolidated batch.

        Uses SmartLedger dependency graph to order instructions correctly:
        - Prerequisites are satisfied before dependents
        - Parallel-safe tasks are grouped together
        - Sequential chains maintain proper order

        Falls back to priority+tag ordering when ledger is unavailable.

        Args:
            max_tokens: Maximum estimated tokens for the batch prompt
            max_instructions: Maximum number of instructions in one batch

        Returns:
            InstructionBatch if there are pending instructions, else None
        """
        with self._lock:
            # Use ledger-aware ordering when available
            pending = self._get_ledger_ordered_pending()
            if not pending:
                return None

            # Select instructions that fit within token budget
            selected = []
            total_chars = 0
            char_budget = max_tokens * 4  # ~4 chars per token

            for inst in pending[:max_instructions]:
                inst_chars = len(inst.text) + len(json.dumps(inst.context))
                if total_chars + inst_chars > char_budget and selected:
                    break  # Stop adding — budget exceeded
                selected.append(inst)
                total_chars += inst_chars

            if not selected:
                return None

            # Generate batch ID
            batch_id = hashlib.sha256(
                f"batch:{self.user_id}:{time.time()}".encode()
            ).hexdigest()[:12]

            # Consolidate into a single prompt
            prompt = self._consolidate(selected)

            # Mark as batched
            for inst in selected:
                inst.status = InstructionStatus.BATCHED
                inst.batch_id = batch_id
                inst.updated_at = datetime.utcnow().isoformat()

            self._save()

            batch = InstructionBatch(batch_id, selected, prompt)
            logger.info(
                f"Batch [{batch_id}]: {len(selected)} instructions, "
                f"~{batch.token_estimate} tokens"
            )
            return batch

    def pull_execution_plan(self, max_tokens: int = 8000,
                            max_instructions: int = 20) -> Optional[ExecutionPlan]:
        """Pull queued instructions as a dependency-aware execution plan.

        Returns an ExecutionPlan with waves of instructions:
        - Each wave is a group of independent instructions (dispatch in parallel)
        - Waves are ordered by dependency (wave N+1 depends on wave N)

        When SmartLedger is unavailable, all instructions go into a single wave
        (effectively the same as pull_batch but wrapped in ExecutionPlan).

        Args:
            max_tokens: Maximum estimated tokens across all waves
            max_instructions: Maximum total instructions

        Returns:
            ExecutionPlan with parallel waves, or None if queue empty
        """
        with self._lock:
            pending = self.get_pending()
            if not pending:
                return None

            # Budget filter
            selected = []
            total_chars = 0
            char_budget = max_tokens * 4
            for inst in pending[:max_instructions]:
                inst_chars = len(inst.text) + len(json.dumps(inst.context))
                if total_chars + inst_chars > char_budget and selected:
                    break
                selected.append(inst)
                total_chars += inst_chars

            if not selected:
                return None

            batch_id = hashlib.sha256(
                f"plan:{self.user_id}:{time.time()}".encode()
            ).hexdigest()[:12]

            # Build waves using ledger dependency graph
            waves = self._build_waves(selected)

            # Mark all as batched
            for inst in selected:
                inst.status = InstructionStatus.BATCHED
                inst.batch_id = batch_id
                inst.updated_at = datetime.utcnow().isoformat()
            self._save()

            plan = ExecutionPlan(waves, batch_id)
            logger.info(
                f"Execution plan [{batch_id}]: {plan.total_instructions} "
                f"instructions in {len(waves)} waves"
            )
            return plan

    def _build_waves(self, instructions: List[Instruction]) -> List[List[Instruction]]:
        """Split instructions into dependency-ordered parallel waves.

        Uses SmartLedger to determine which instructions are independent
        (can run in same wave) vs dependent (must run in later waves).

        Wave 0: all instructions with no prerequisites
        Wave 1: instructions whose prerequisites are all in wave 0
        Wave N: instructions whose prerequisites are all in waves < N

        Falls back to single wave when ledger is unavailable.
        """
        ledger = self._get_ledger()
        if ledger is None or not self._task_map:
            # No ledger — everything in one wave
            return [instructions]

        # Build maps
        inst_by_id: Dict[str, Instruction] = {i.id: i for i in instructions}
        inst_by_task: Dict[str, Instruction] = {}
        task_by_inst: Dict[str, str] = {}
        unmapped: List[Instruction] = []

        for inst in instructions:
            ltid = self._task_map.get(inst.id)
            if ltid and ltid in ledger.tasks:
                inst_by_task[ltid] = inst
                task_by_inst[inst.id] = ltid
            else:
                unmapped.append(inst)

        if not inst_by_task:
            return [instructions]

        # Collect task IDs that are in our instruction set
        our_task_ids = set(inst_by_task.keys())

        # Build dependency graph restricted to our task set
        # For each task, find which of OUR tasks it depends on
        deps: Dict[str, set] = {}  # task_id → set of task_ids it depends on
        for tid in our_task_ids:
            task = ledger.tasks.get(tid)
            if task is None:
                deps[tid] = set()
                continue
            task_deps = set()
            # Check prerequisites
            if hasattr(task, 'prerequisites') and task.prerequisites:
                for prereq in task.prerequisites:
                    if prereq in our_task_ids:
                        task_deps.add(prereq)
            # Check blocked_by
            if hasattr(task, 'blocked_by') and task.blocked_by:
                for blocker in task.blocked_by:
                    if blocker in our_task_ids:
                        task_deps.add(blocker)
            # Check parent (child must wait for parent's other children)
            if hasattr(task, 'parent_task_id') and task.parent_task_id:
                if task.parent_task_id in our_task_ids:
                    task_deps.add(task.parent_task_id)
            deps[tid] = task_deps

        # Topological sort into waves (Kahn's algorithm by level)
        waves: List[List[Instruction]] = []
        placed: set = set()
        remaining = set(our_task_ids)

        max_iterations = len(our_task_ids) + 1
        for _ in range(max_iterations):
            if not remaining:
                break
            # Find tasks whose deps are all placed
            wave_tasks = [
                tid for tid in remaining
                if deps[tid].issubset(placed)
            ]
            if not wave_tasks:
                # Circular dependency — dump remaining into last wave
                wave_tasks = list(remaining)
            wave = [inst_by_task[tid] for tid in wave_tasks if tid in inst_by_task]
            if wave:
                waves.append(wave)
            placed.update(wave_tasks)
            remaining -= set(wave_tasks)

        # Unmapped instructions go in the first wave (no known deps)
        if unmapped:
            if waves:
                waves[0] = unmapped + waves[0]
            else:
                waves = [unmapped]

        return waves if waves else [instructions]

    def complete_instruction(self, instruction_id: str, result: Optional[str] = None):
        """Mark a single instruction as done and notify ledger.

        Used by parallel dispatch to complete individual instructions
        (vs complete_batch which completes all at once).
        """
        with self._lock:
            inst = self._instructions.get(instruction_id)
            if not inst:
                return
            inst.status = InstructionStatus.DONE
            inst.updated_at = datetime.utcnow().isoformat()
            if result:
                inst.result = result
            # Notify ledger — unblocks dependents
            ledger = self._get_ledger()
            if ledger is not None:
                ltid = self._task_map.get(instruction_id)
                if ltid:
                    try:
                        ledger.complete_task_and_route(
                            ltid, outcome='success', result=result,
                        )
                    except Exception as e:
                        logger.debug(f"Ledger completion failed for [{ltid}]: {e}")
            self._save()

    def fail_instruction(self, instruction_id: str, error: str):
        """Mark a single instruction as failed and return to queue."""
        with self._lock:
            inst = self._instructions.get(instruction_id)
            if not inst:
                return
            inst.status = InstructionStatus.QUEUED
            inst.batch_id = None
            inst.error = error
            inst.updated_at = datetime.utcnow().isoformat()
            ledger = self._get_ledger()
            if ledger is not None:
                ltid = self._task_map.get(instruction_id)
                if ltid:
                    try:
                        ledger.complete_task_and_route(
                            ltid, outcome='failure', result=error,
                        )
                    except Exception as e:
                        logger.debug(f"Ledger failure routing for [{ltid}]: {e}")
            self._save()

    def _consolidate(self, instructions: List[Instruction]) -> str:
        """Consolidate multiple instructions into a single prompt.

        Groups by tags, adds context, maintains priority order.
        """
        if len(instructions) == 1:
            inst = instructions[0]
            prompt = inst.text
            if inst.context:
                ctx_str = json.dumps(inst.context, indent=2)
                prompt = f"Context:\n{ctx_str}\n\nInstruction:\n{inst.text}"
            return prompt

        # Multiple instructions → structured batch
        lines = [
            f"You have {len(instructions)} queued instructions to execute.",
            "Process them in the order listed. Each instruction may depend on previous ones.",
            "",
        ]

        # Group by tags
        tagged: Dict[str, List[Instruction]] = {}
        untagged: List[Instruction] = []
        for inst in instructions:
            if inst.tags:
                key = ', '.join(sorted(inst.tags))
                tagged.setdefault(key, []).append(inst)
            else:
                untagged.append(inst)

        idx = 1
        if tagged:
            for tag_group, group_insts in tagged.items():
                lines.append(f"## Group: {tag_group}")
                for inst in group_insts:
                    lines.append(f"{idx}. [P{inst.priority}] {inst.text}")
                    if inst.context:
                        lines.append(f"   Context: {json.dumps(inst.context)}")
                    idx += 1
                lines.append("")

        if untagged:
            if tagged:
                lines.append("## Other Instructions")
            for inst in untagged:
                lines.append(f"{idx}. [P{inst.priority}] {inst.text}")
                if inst.context:
                    lines.append(f"   Context: {json.dumps(inst.context)}")
                idx += 1

        return '\n'.join(lines)

    def complete_batch(self, batch_id: str, result: Optional[str] = None):
        """Mark all instructions in a batch as done.

        Also calls ledger.complete_task_and_route() for each instruction
        so the SmartLedger can unblock dependent tasks and activate
        conditional/sequential follow-ups.
        """
        with self._lock:
            ledger = self._get_ledger()
            for inst in self._instructions.values():
                if inst.batch_id == batch_id:
                    inst.status = InstructionStatus.DONE
                    inst.updated_at = datetime.utcnow().isoformat()
                    if result:
                        inst.result = result
                    # Notify ledger — unblocks dependents
                    if ledger is not None:
                        ltid = self._task_map.get(inst.id)
                        if ltid:
                            try:
                                ledger.complete_task_and_route(
                                    ltid, outcome='success', result=result,
                                )
                            except Exception as e:
                                logger.debug(f"Ledger completion failed for [{ltid}]: {e}")
            self._save()

    def fail_batch(self, batch_id: str, error: str):
        """Mark batch instructions as failed — they return to QUEUED for retry.

        Also notifies the ledger so dependent tasks remain blocked
        until the retry succeeds.
        """
        with self._lock:
            ledger = self._get_ledger()
            for inst in self._instructions.values():
                if inst.batch_id == batch_id and inst.status == InstructionStatus.BATCHED:
                    inst.status = InstructionStatus.QUEUED
                    inst.batch_id = None
                    inst.error = error
                    inst.updated_at = datetime.utcnow().isoformat()
                    # Notify ledger of failure
                    if ledger is not None:
                        ltid = self._task_map.get(inst.id)
                        if ltid:
                            try:
                                ledger.complete_task_and_route(
                                    ltid, outcome='failure', result=error,
                                )
                            except Exception as e:
                                logger.debug(f"Ledger failure routing failed for [{ltid}]: {e}")
            self._save()

    def stats(self) -> Dict:
        """Queue statistics."""
        with self._lock:
            statuses = {}
            for inst in self._instructions.values():
                s = inst.status.value if isinstance(inst.status, InstructionStatus) else inst.status
                statuses[s] = statuses.get(s, 0) + 1
            return {
                'user_id': self.user_id,
                'total': len(self._instructions),
                'by_status': statuses,
                'pending': statuses.get('queued', 0),
            }

    def clear_done(self):
        """Remove completed/cancelled instructions to prevent unbounded growth."""
        with self._lock:
            to_remove = [
                iid for iid, inst in self._instructions.items()
                if inst.status in (InstructionStatus.DONE, InstructionStatus.CANCELLED)
            ]
            for iid in to_remove:
                del self._instructions[iid]
            if to_remove:
                self._save()
            return len(to_remove)


# ═══════════════════════════════════════════════════════════════════════
# Singleton registry — one queue per user
# ═══════════════════════════════════════════════════════════════════════

_queues: Dict[str, InstructionQueue] = {}
_queue_lock = threading.Lock()


def get_queue(user_id: str) -> InstructionQueue:
    """Get or create instruction queue for a user."""
    with _queue_lock:
        if user_id not in _queues:
            _queues[user_id] = InstructionQueue(user_id)
        return _queues[user_id]


def enqueue_instruction(user_id: str, text: str, **kwargs) -> Instruction:
    """Convenience: enqueue an instruction for a user."""
    return get_queue(user_id).enqueue(text, **kwargs)


def pull_user_batch(user_id: str, max_tokens: int = 8000) -> Optional[InstructionBatch]:
    """Convenience: pull a batch for a user."""
    return get_queue(user_id).pull_batch(max_tokens=max_tokens)


def get_all_pending() -> Dict[str, List[Dict]]:
    """Get pending instructions across all users."""
    result = {}
    # Scan queue directory for all users
    if os.path.isdir(_QUEUE_DIR):
        for fname in os.listdir(_QUEUE_DIR):
            if fname.endswith('_queue.json'):
                uid = fname.replace('_queue.json', '')
                q = get_queue(uid)
                pending = q.get_pending()
                if pending:
                    result[uid] = [i.to_dict() for i in pending]
    return result
