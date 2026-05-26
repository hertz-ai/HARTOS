"""
Hive Task Protocol — Define, dispatch, and track distributed coding tasks.

Tasks flow through the hive:
  1. A seeded agent (or user) creates a HiveTask
  2. HiveTaskDispatcher finds the best available Claude Code session
  3. Task is shard-filtered based on trust level
  4. Session executes and reports result
  5. Result is validated, Spark reward calculated, capital distributed

Task types map to seeded agents:
  - CODE_REVIEW: Review PR/code for quality, security
  - CODE_WRITE: Write new code for a feature/fix
  - CODE_TEST: Write or run tests
  - MODEL_ONBOARD: Quantize + onboard a new HF model
  - BENCHMARK: Run benchmarks on a model
  - DOCUMENTATION: Write docs for code
  - BUG_FIX: Fix a reported bug
  - REFACTOR: Improve code structure

Storage: agent_data/hive_tasks.json (portable across nodes).
Thread-safe via Lock on all mutations.
"""

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── Storage path ───────────────────────────────────────────────────────
# Must be user-writable.  Previously resolved to the HART_INSTALL_DIR
# fallback which in bundled Nunba builds is `C:\Program Files (x86)\
# HevolveAI\Nunba` — read-only for non-admin users, causing every
# `_save()` to fail with `[Errno 13] Permission denied` (task #250
# regression found by runtime-log-watcher on 2026-04-15).  Use the
# platform-standard user data dir: `~/Documents/Nunba/data/agent_data`
# on all OSes, matching MemoryGraph + every other Nunba data path.
def _resolve_data_dir() -> str:
    try:
        from core.platform_paths import get_data_dir as _gdd
        _base = _gdd()
    except Exception:
        _base = os.path.join(
            os.path.expanduser('~'), 'Documents', 'Nunba', 'data',
        )
    _dir = os.path.join(_base, 'agent_data')
    try:
        os.makedirs(_dir, exist_ok=True)
    except OSError:
        pass
    return _dir


_DATA_DIR = _resolve_data_dir()
_TASKS_FILE = os.path.join(_DATA_DIR, 'hive_tasks.json')


# ─── Task Types ─────────────────────────────────────────────────────────

class HiveTaskType(str, Enum):
    CODE_REVIEW = 'code_review'
    CODE_WRITE = 'code_write'
    CODE_TEST = 'code_test'
    MODEL_ONBOARD = 'model_onboard'
    BENCHMARK = 'benchmark'
    DOCUMENTATION = 'documentation'
    BUG_FIX = 'bug_fix'
    REFACTOR = 'refactor'


class HiveTaskStatus(str, Enum):
    PENDING = 'pending'
    ASSIGNED = 'assigned'
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    FAILED = 'failed'
    VALIDATED = 'validated'
    CANCELLED = 'cancelled'


# Map seeded-agent slugs to the task types they create.
# The daemon can use this to auto-create tasks from active goals.
AGENT_TASK_MAP = {
    'bootstrap_compute_recruiter': HiveTaskType.CODE_WRITE,
    'bootstrap_model_provisioner': HiveTaskType.MODEL_ONBOARD,
    'bootstrap_capital_distributor': HiveTaskType.CODE_WRITE,
    'bootstrap_hive_model_trainer': HiveTaskType.BENCHMARK,
    'bootstrap_opensource_evangelist': HiveTaskType.DOCUMENTATION,
    'bootstrap_node_health_optimizer': HiveTaskType.BUG_FIX,
}

# Base Spark reward ranges per task type (min, max).
_SPARK_RANGES = {
    HiveTaskType.CODE_REVIEW: (5, 30),
    HiveTaskType.CODE_WRITE: (10, 100),
    HiveTaskType.CODE_TEST: (8, 50),
    HiveTaskType.MODEL_ONBOARD: (15, 80),
    HiveTaskType.BENCHMARK: (5, 40),
    HiveTaskType.DOCUMENTATION: (3, 25),
    HiveTaskType.BUG_FIX: (10, 80),
    HiveTaskType.REFACTOR: (10, 60),
}


# ─── Dataclass ──────────────────────────────────────────────────────────

@dataclass
class HiveTask:
    """A single distributed coding task for the hive."""

    task_id: str                          # UUID4
    task_type: str                        # HiveTaskType value
    title: str
    description: str
    instructions: str                     # Detailed instructions for Claude Code
    repo_url: str = ''                    # Git repo URL (empty for hive-internal)
    files_scope: List[str] = field(default_factory=list)   # Files involved
    shard_level: str = 'INTERFACES'       # Privacy level for untrusted sessions
    priority: int = 50                    # 0-100 (higher = more urgent)
    spark_reward: int = 10                # Spark tokens for completion
    max_duration_minutes: int = 30
    requires_tests: bool = True
    requires_review: bool = True
    origin_node_id: str = ''              # Who created this task
    origin_signature: str = ''            # Ed25519 signature for verification
    assigned_session_id: str = ''
    status: str = 'pending'               # HiveTaskStatus value
    result: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> 'HiveTask':
        """Reconstruct from a JSON-serialised dict, tolerating missing keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


# ─── Helpers ────────────────────────────────────────────────────────────

def estimate_complexity(instructions: str) -> int:
    """Heuristic complexity estimate. Returns a Spark value 1-100.

    Signals:
      - Instruction length (more text = more work)
      - File references (paths ending in .py, .js, etc.)
      - Presence of testing requirements
      - Presence of refactoring/migration keywords
    """
    score = 0

    # Length component: 1 point per 200 characters, capped at 40
    score += min(40, len(instructions) // 200)

    instructions_lower = instructions.lower()

    # File references
    file_extensions = ('.py', '.js', '.ts', '.rs', '.go', '.java', '.c', '.cpp')
    file_count = sum(
        1 for word in instructions.split()
        if any(word.endswith(ext) for ext in file_extensions)
    )
    score += min(20, file_count * 3)

    # Testing requirements
    test_keywords = ('test', 'pytest', 'unittest', 'coverage', 'assert')
    if any(kw in instructions_lower for kw in test_keywords):
        score += 10

    # Refactoring/migration keywords
    hard_keywords = ('refactor', 'migrate', 'rewrite', 'restructure',
                     'breaking change', 'backward compat')
    if any(kw in instructions_lower for kw in hard_keywords):
        score += 15

    # Security keywords
    security_keywords = ('security', 'vulnerability', 'cve', 'injection',
                         'authentication', 'authorization')
    if any(kw in instructions_lower for kw in security_keywords):
        score += 10

    return max(1, min(100, score))


def validate_result(task: HiveTask, result: Dict) -> float:
    """Quality score 0.0-1.0 for a task result.

    Checks:
      - Files changed match the task scope
      - Tests included if task requires them
      - No PII leakage (DLP scan)
      - Result dict contains expected keys
    """
    score = 0.0
    checks_total = 0

    # 1. Result structure: must have 'files_changed' or 'diff'
    checks_total += 1
    if result.get('files_changed') or result.get('diff'):
        score += 1.0

    # 2. Files match scope (if scope was defined)
    if task.files_scope:
        checks_total += 1
        changed = set(result.get('files_changed', []))
        scope = set(task.files_scope)
        if changed and changed.issubset(scope):
            score += 1.0
        elif changed:
            # Partial credit: fraction within scope
            overlap = len(changed & scope)
            score += overlap / len(changed) if changed else 0.0

    # 3. Tests included when required
    if task.requires_tests:
        checks_total += 1
        if result.get('tests_passed') is not None:
            score += 1.0 if result['tests_passed'] else 0.3
        elif result.get('test_output'):
            score += 0.5  # Tests ran but no pass/fail indicator

    # 4. No errors reported
    checks_total += 1
    if not result.get('error'):
        score += 1.0

    # 5. DLP scan on result text (try/except — optional dependency)
    result_text = json.dumps(result, default=str)
    checks_total += 1
    try:
        from security.dlp_engine import get_dlp_engine
        dlp = get_dlp_engine()
        findings = dlp.scan(result_text)
        if not findings:
            score += 1.0
        else:
            logger.warning(
                "DLP findings in task %s result: %d PII items",
                task.task_id, len(findings),
            )
            # Partial credit: penalise proportionally
            score += max(0.0, 1.0 - len(findings) * 0.25)
    except Exception:
        # DLP not available — give benefit of the doubt
        score += 1.0

    return round(score / checks_total, 3) if checks_total > 0 else 0.0


# ─── Persistence ────────────────────────────────────────────────────────

def _load_tasks() -> List[Dict]:
    """Load task list from JSON file."""
    if not os.path.exists(_TASKS_FILE):
        return []
    try:
        with open(_TASKS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Failed to load hive tasks: %s", exc)
        return []


def _save_tasks(tasks: List[Dict]) -> None:
    """Atomically save task list to JSON file."""
    os.makedirs(os.path.dirname(_TASKS_FILE), exist_ok=True)
    tmp_path = _TASKS_FILE + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, indent=2, default=str)
        # Atomic rename (works on POSIX; best-effort on Windows)
        if os.path.exists(_TASKS_FILE):
            os.replace(tmp_path, _TASKS_FILE)
        else:
            os.rename(tmp_path, _TASKS_FILE)
    except IOError as exc:
        logger.error("Failed to save hive tasks: %s", exc)


# ─── Dispatcher ─────────────────────────────────────────────────────────

class HiveTaskDispatcher:
    """Create, dispatch, and track distributed coding tasks.

    Finds pending tasks, matches them to connected Claude Code sessions
    based on capabilities and trust, then tracks results and distributes
    Spark rewards via the revenue aggregator.

    Thread-safe: all task mutations are guarded by ``_lock``.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: Dict[str, HiveTask] = {}
        self._stats = {
            'total_created': 0,
            'total_completed': 0,
            'total_failed': 0,
            'total_spark_distributed': 0,
            'quality_scores': [],  # Rolling window for avg
        }
        self._load_from_disk()

    # ── Persistence ──────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        raw = _load_tasks()
        for d in raw:
            try:
                task = HiveTask.from_dict(d)
                self._tasks[task.task_id] = task
            except Exception as exc:
                logger.debug("Skipping malformed task: %s", exc)

    def _persist(self) -> None:
        """Save current task state to disk. Caller must hold _lock."""
        _save_tasks([t.to_dict() for t in self._tasks.values()])

    # ── Task creation ────────────────────────────────────────────────

    def create_task(self, task_type: str, title: str, description: str,
                    instructions: str, **kwargs) -> HiveTask:
        """Create and queue a new hive task.

        Auto-calculates ``spark_reward`` from instruction complexity if
        not explicitly provided via *kwargs*.

        Args:
            task_type: One of HiveTaskType values.
            title: Short human-readable title.
            description: What the task accomplishes.
            instructions: Detailed instructions for Claude Code.
            **kwargs: Override any HiveTask field (priority, spark_reward, etc.)

        Returns:
            The newly created HiveTask (status='pending').
        """
        task_id = str(uuid.uuid4())

        # Auto-calculate Spark reward if not provided
        if 'spark_reward' not in kwargs:
            complexity = estimate_complexity(instructions)
            try:
                tt = HiveTaskType(task_type)
                lo, hi = _SPARK_RANGES.get(tt, (5, 50))
            except ValueError:
                lo, hi = 5, 50
            kwargs['spark_reward'] = max(lo, min(hi, complexity))

        task = HiveTask(
            task_id=task_id,
            task_type=task_type,
            title=title,
            description=description,
            instructions=instructions,
            created_at=time.time(),
            **kwargs,
        )

        with self._lock:
            self._tasks[task_id] = task
            self._stats['total_created'] += 1
            self._persist()

        logger.info(
            "Hive task created: [%s] %s (type=%s, spark=%d, priority=%d)",
            task_id[:8], title, task_type, task.spark_reward, task.priority,
        )
        return task

    # ── Dispatch ─────────────────────────────────────────────────────

    def dispatch_pending(self) -> int:
        """Find pending tasks and dispatch to available sessions.

        Called by the agent daemon on each tick. Matches tasks to the
        best available Claude Code session based on capabilities and
        trust level, then calls ``session.receive_task()``.

        Returns:
            Number of tasks successfully dispatched.
        """
        dispatched = 0
        pending = self.get_pending_tasks()
        if not pending:
            return 0

        for task in pending:
            session_id = self.match_session(task)
            if not session_id:
                continue

            # Deliver task to session
            delivered = self._deliver_to_session(session_id, task)
            if delivered:
                with self._lock:
                    task.status = HiveTaskStatus.ASSIGNED.value
                    task.assigned_session_id = session_id
                    self._persist()
                dispatched += 1
                logger.info(
                    "Dispatched task [%s] -> session [%s]",
                    task.task_id[:8], session_id[:8],
                )

        return dispatched

    def match_session(self, task: HiveTask) -> Optional[str]:
        """Find the best session for a task.

        Selection criteria (in priority order):
          1. Filter by task scope (own_repos vs public vs any)
          2. Filter by capabilities (language, framework match)
          3. Prefer sessions with higher quality scores
          4. Prefer sessions with lower latency (same region)

        Returns:
            session_id or None if no suitable session found.
        """
        try:
            from integrations.coding_agent.claude_hive_session import (
                get_session_registry,
            )
            registry = get_session_registry()
        except ImportError:
            logger.debug("claude_hive_session not available for matching")
            return None
        except Exception as exc:
            logger.debug("Session registry unavailable: %s", exc)
            return None

        if not hasattr(registry, 'get_available_sessions'):
            return None

        try:
            sessions = registry.get_available_sessions()
        except Exception:
            return None

        if not sessions:
            return None

        # Score each session
        best_id = None
        best_score = -1.0

        for session in sessions:
            sid = session.get('session_id', '')
            if not sid:
                continue

            score = 0.0

            # Capability match: language overlap
            session_langs = set(session.get('languages', []))
            task_langs = set()
            for fpath in task.files_scope:
                ext = os.path.splitext(fpath)[1].lower()
                lang_map = {
                    '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
                    '.rs': 'rust', '.go': 'go', '.java': 'java',
                    '.c': 'c', '.cpp': 'cpp',
                }
                if ext in lang_map:
                    task_langs.add(lang_map[ext])
            if not task_langs or task_langs & session_langs:
                score += 2.0  # Match or no constraint

            # Quality score
            quality = session.get('quality_score', 0.5)
            score += quality * 3.0

            # Latency / region preference
            session_region = session.get('region', '')
            task_region = task.origin_node_id[:3] if task.origin_node_id else ''
            if session_region and task_region and session_region == task_region:
                score += 1.0

            # Availability: prefer idle sessions
            if session.get('status') == 'idle':
                score += 1.5

            if score > best_score:
                best_score = score
                best_id = sid

        return best_id

    def _deliver_to_session(self, session_id: str, task: HiveTask) -> bool:
        """Send a task to a Claude Code session. Returns True on success."""
        try:
            from integrations.coding_agent.claude_hive_session import (
                get_session_registry,
            )
            registry = get_session_registry()
            session = registry.get_session(session_id)
            if session is None:
                return False

            # Apply shard filtering based on trust level
            task_payload = task.to_dict()
            session_trust = getattr(session, 'trust_level', 'PEER')
            if session_trust not in ('SAME_USER', 'FULL_FILE'):
                task_payload = self._apply_shard_filter(task, session_trust)

            return session.receive_task(task_payload)
        except ImportError:
            logger.debug("Cannot deliver: claude_hive_session not importable")
            return False
        except Exception as exc:
            logger.warning("Task delivery failed for [%s]: %s",
                           task.task_id[:8], exc)
            return False

    def _apply_shard_filter(self, task: HiveTask, trust_level: str) -> Dict:
        """Reduce task payload based on shard scope for untrusted sessions.

        Uses the ShardEngine to strip implementation details, leaving
        only interface-level information for untrusted peers.
        """
        payload = task.to_dict()
        try:
            from integrations.agent_engine.shard_engine import (
                ShardEngine, ShardScope,
            )
            scope_map = {
                'INTERFACES': ShardScope.INTERFACES,
                'SIGNATURES': ShardScope.SIGNATURES,
                'MINIMAL': ShardScope.MINIMAL,
            }
            scope = scope_map.get(task.shard_level, ShardScope.INTERFACES)
            engine = ShardEngine()
            shard = engine.create_shard(
                task=task.instructions,
                target_files=task.files_scope,
                scope=scope,
            )
            payload['shard'] = shard.to_dict()
            # Strip raw instructions for untrusted sessions
            if scope != ShardScope.FULL_FILE:
                payload.pop('instructions', None)
        except Exception as exc:
            logger.debug("Shard filtering failed, sending metadata only: %s", exc)
            payload.pop('instructions', None)
        return payload

    # ── Result handling ──────────────────────────────────────────────

    def on_task_result(self, task_id: str, result: Dict) -> Dict:
        """Called when a session reports a task result.

        Validates the result, calculates a quality score, awards Spark
        via the revenue aggregator, and updates task status.

        Args:
            task_id: UUID of the completed task.
            result: Dict with keys like files_changed, diff, tests_passed,
                    test_output, error, etc.

        Returns:
            {spark_awarded: int, quality_score: float, validated: bool}
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {'spark_awarded': 0, 'quality_score': 0.0,
                        'validated': False, 'error': 'unknown_task'}

            quality = validate_result(task, result)
            task.result = result
            task.completed_at = time.time()

            # Quality gate: require >= 0.4 to count as completed
            validated = quality >= 0.4
            if validated:
                task.status = HiveTaskStatus.VALIDATED.value
            else:
                task.status = HiveTaskStatus.FAILED.value
                self._stats['total_failed'] += 1

            # Calculate Spark reward: base * quality
            spark_awarded = 0
            if validated:
                spark_awarded = max(1, int(task.spark_reward * quality))
                self._stats['total_completed'] += 1
                self._stats['total_spark_distributed'] += spark_awarded
                self._distribute_spark(task, spark_awarded)

            self._stats['quality_scores'].append(quality)
            # Keep rolling window of last 500 scores
            if len(self._stats['quality_scores']) > 500:
                self._stats['quality_scores'] = self._stats['quality_scores'][-500:]

            self._persist()

        logger.info(
            "Task result [%s]: quality=%.3f, spark=%d, validated=%s",
            task_id[:8], quality, spark_awarded, validated,
        )
        return {
            'spark_awarded': spark_awarded,
            'quality_score': quality,
            'validated': validated,
        }

    def _distribute_spark(self, task: HiveTask, spark_amount: int) -> None:
        """Award Spark to the session operator via revenue aggregator.

        Follows the 90/9/1 split model:
          90% to the compute contributor (session operator)
           9% to infrastructure pool
           1% to central
        """
        try:
            from integrations.agent_engine.revenue_aggregator import (
                REVENUE_SPLIT_USERS,
                REVENUE_SPLIT_INFRA,
                REVENUE_SPLIT_CENTRAL,
            )
            user_share = max(1, int(spark_amount * REVENUE_SPLIT_USERS))
            infra_share = int(spark_amount * REVENUE_SPLIT_INFRA)
            central_share = int(spark_amount * REVENUE_SPLIT_CENTRAL)

            logger.info(
                "Spark distribution for task [%s]: "
                "user=%d, infra=%d, central=%d (total=%d)",
                task.task_id[:8], user_share, infra_share, central_share,
                spark_amount,
            )
            # Actual wallet crediting would go through ResonanceService.award_spark()
            # when the social models are available. Log-only for now.
        except ImportError:
            logger.debug("revenue_aggregator not available for Spark distribution")
        except Exception as exc:
            logger.warning("Spark distribution failed: %s", exc)

    # ── Query ────────────────────────────────────────────────────────

    def get_pending_tasks(self) -> List[HiveTask]:
        """List all pending tasks sorted by priority (highest first)."""
        with self._lock:
            pending = [
                t for t in self._tasks.values()
                if t.status == HiveTaskStatus.PENDING.value
            ]
        pending.sort(key=lambda t: (-t.priority, t.created_at))
        return pending

    def get_task(self, task_id: str) -> Optional[HiveTask]:
        """Retrieve a task by ID."""
        with self._lock:
            return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending or assigned task. Returns True if cancelled."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (HiveTaskStatus.COMPLETED.value,
                               HiveTaskStatus.VALIDATED.value):
                return False  # Cannot cancel finished tasks
            task.status = HiveTaskStatus.CANCELLED.value
            self._persist()
        logger.info("Task [%s] cancelled", task_id[:8])
        return True

    def get_stats(self) -> Dict:
        """Dispatcher statistics.

        Returns:
            Dict with total_created, total_completed, total_failed,
            avg_quality, total_spark_distributed, pending_count.
        """
        with self._lock:
            scores = self._stats['quality_scores']
            avg_quality = (
                round(sum(scores) / len(scores), 3) if scores else 0.0
            )
            return {
                'total_created': self._stats['total_created'],
                'total_completed': self._stats['total_completed'],
                'total_failed': self._stats['total_failed'],
                'total_spark_distributed': self._stats['total_spark_distributed'],
                'avg_quality': avg_quality,
                'pending_count': sum(
                    1 for t in self._tasks.values()
                    if t.status == HiveTaskStatus.PENDING.value
                ),
                'active_count': sum(
                    1 for t in self._tasks.values()
                    if t.status in (HiveTaskStatus.ASSIGNED.value,
                                    HiveTaskStatus.IN_PROGRESS.value)
                ),
                'total_tasks': len(self._tasks),
            }


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

_dispatcher: Optional[HiveTaskDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_dispatcher() -> HiveTaskDispatcher:
    """Get or create the HiveTaskDispatcher singleton."""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = HiveTaskDispatcher()
    return _dispatcher
