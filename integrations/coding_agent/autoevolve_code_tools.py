"""
Auto-Evolve Code Tools — Agent-native code experiment tools.

Individual tools for autonomous code experiments: setup → edit → run → score →
keep/revert → finalize. The agent's conversation loop (autogen group chat)
drives iteration — no hardcoded Python while loop.

Inspired by karpathy/autoresearch. Each tool is a single step:
    1. autoresearch_setup    — create session, run baseline, return session_id
    2. autoresearch_edit     — LLM proposes + applies one code edit
    3. autoresearch_run      — run experiment, extract metric, record benchmark
    4. autoresearch_decide   — keep (git commit) or revert (git checkout)
    5. autoresearch_finalize — save report, export learning delta
    6. get_autoresearch_status — poll session progress

Uses existing infra only:
    - AiderNativeBackend for code edits
    - run_cmd_subprocess for experiment execution
    - BenchmarkTracker for score tracking
    - CodingRecipeBridge for saving winning edits as recipes
    - AgentBaselineService for evolution snapshots
    - EventBus for live progress events
"""
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.autoresearch')


# ── Result Types ─────────────────────────────────────────────

@dataclass
class ExperimentResult:
    """Result of a single experiment iteration."""
    iteration: int
    hypothesis: str
    metric_name: str
    metric_value: Optional[float]
    baseline_value: Optional[float]
    improved: bool
    files_changed: List[str] = field(default_factory=list)
    edits: List[Dict] = field(default_factory=list)
    run_output: str = ''
    error: str = ''
    duration_s: float = 0.0

    @property
    def delta(self) -> Optional[float]:
        if self.metric_value is not None and self.baseline_value is not None:
            return self.metric_value - self.baseline_value
        return None


@dataclass
class AutoResearchSession:
    """Tracks the full autoresearch session state."""
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    experiment_id: str = ''        # ThoughtExperiment ID (if triggered by one)
    goal_id: str = ''              # AgentGoal ID
    repo_path: str = ''            # Working directory
    target_file: str = ''          # The file being modified (like train.py)
    run_command: str = ''          # Command to run the experiment
    metric_name: str = 'score'     # Name of the metric to optimize
    metric_pattern: str = ''       # Regex to extract metric from output
    metric_direction: str = 'higher_is_better'  # or 'lower_is_better'
    max_iterations: int = 50
    time_budget_s: int = 300       # Per-iteration time budget (5 min default)
    spark_budget: int = 200        # Total Spark budget
    spark_consumed: int = 0
    spark_per_iteration: int = 4   # Spark cost per iteration

    # State
    baseline_metric: Optional[float] = None
    best_metric: Optional[float] = None
    best_iteration: int = 0
    current_iteration: int = 0
    status: str = 'pending'  # pending | running | completed | failed | budget_exhausted
    results: List[Dict] = field(default_factory=list)
    start_time: float = 0.0
    total_improvements: int = 0
    # Last edit state (for decide step)
    _pending_edits: List[Dict] = field(default_factory=list)
    _pending_files: List[str] = field(default_factory=list)
    _pending_hypothesis: str = ''

    def is_budget_exhausted(self) -> bool:
        return self.spark_consumed + self.spark_per_iteration > self.spark_budget

    def is_improved(self, new_val: float) -> bool:
        if self.best_metric is None:
            return True
        if self.metric_direction == 'lower_is_better':
            return new_val < self.best_metric
        return new_val > self.best_metric

    def to_progress_dict(self) -> Dict:
        return {
            'session_id': self.session_id,
            'status': self.status,
            'iteration': self.current_iteration,
            'max_iterations': self.max_iterations,
            'baseline_metric': self.baseline_metric,
            'best_metric': self.best_metric,
            'best_iteration': self.best_iteration,
            'total_improvements': self.total_improvements,
            'spark_consumed': self.spark_consumed,
            'spark_budget': self.spark_budget,
            'elapsed_s': time.time() - self.start_time if self.start_time else 0,
        }


# ── Engine (session store + utilities) ────────────────────────

class AutoResearchEngine:
    """Session store and utility methods for autoresearch tools.

    NOT a loop — the agent's conversation drives iteration by calling
    individual tool functions in sequence.
    """

    def __init__(self):
        self._active_sessions: Dict[str, AutoResearchSession] = {}
        self._lock = threading.Lock()

    def register_session(self, session: AutoResearchSession):
        with self._lock:
            self._active_sessions[session.session_id] = session

    def unregister_session(self, session_id: str):
        with self._lock:
            self._active_sessions.pop(session_id, None)

    def get_active_sessions(self) -> List[Dict]:
        """Return progress for all active sessions."""
        with self._lock:
            return [s.to_progress_dict() for s in self._active_sessions.values()]

    def get_session(self, session_id: str) -> Optional[AutoResearchSession]:
        with self._lock:
            return self._active_sessions.get(session_id)

    # ── Edit Generation ─────────────────────────────────────

    def generate_and_apply_edit(self, session: AutoResearchSession
                                ) -> Optional[Tuple[str, List[Dict], List[str]]]:
        """Use LLM to generate a hypothesis and code edit, then apply it."""
        try:
            from integrations.coding_agent.aider_native_backend import AiderNativeBackend
            backend = AiderNativeBackend()

            history_summary = self.build_history_summary(session)

            # Query BenchmarkTracker for best-performing tool insights
            benchmark_hint = ''
            try:
                from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
                tracker = get_benchmark_tracker()
                best = tracker.get_best_tool('autoresearch')
                if best:
                    name, success_rate, avg_time = best
                    benchmark_hint = (
                        f"\nBENCHMARK INSIGHT: Best tool '{name}' has "
                        f"{success_rate:.0%} success rate, avg {avg_time:.1f}s.\n"
                    )
            except Exception:
                pass

            task = (
                f"You are running an autonomous research loop.\n\n"
                f"TARGET FILE: {session.target_file}\n"
                f"METRIC: {session.metric_name} "
                f"({'lower is better' if session.metric_direction == 'lower_is_better' else 'higher is better'})\n"
                f"BASELINE: {session.baseline_metric}\n"
                f"CURRENT BEST: {session.best_metric} (iteration {session.best_iteration})\n"
                f"ITERATION: {session.current_iteration}/{session.max_iterations}\n\n"
                f"EXPERIMENT HISTORY:\n{history_summary}\n\n"
                f"{benchmark_hint}"
                f"RUN COMMAND: {session.run_command}\n\n"
                f"YOUR TASK:\n"
                f"1. Analyze what worked and what didn't from the history above\n"
                f"2. Propose ONE focused modification to {session.target_file}\n"
                f"3. Explain your hypothesis in one sentence\n"
                f"4. Make the edit using SEARCH/REPLACE blocks\n\n"
                f"RULES:\n"
                f"- One change per iteration — small, testable, reversible\n"
                f"- If you're stuck, try combinations of previous improvements\n"
                f"- If all ideas seem tried, try something radical or architectural\n"
                f"- Simplicity wins — a 0.001 gain from deleting code beats a 0.001 gain from 20 lines\n"
                f"- NEVER modify the evaluation metric or test harness\n"
            )

            context = {
                'working_dir': session.repo_path,
                'files': [session.target_file],
            }

            result = backend.execute(task, context, timeout=120)

            if not result.get('success'):
                return None

            output = result.get('output', '')
            hypothesis = output.split('\n')[0][:200] if output else 'Unknown hypothesis'
            edits = result.get('edits', [])
            files_changed = result.get('files_changed', [])

            return hypothesis, edits, files_changed

        except Exception as e:
            logger.warning(f"[{session.session_id}] Edit generation failed: {e}")
            return None

    # ── Experiment Execution ─────────────────────────────────

    def run_experiment(self, session: AutoResearchSession,
                       is_baseline: bool = False) -> ExperimentResult:
        """Run the experiment command and extract the metric."""
        import re

        result = ExperimentResult(
            iteration=0 if is_baseline else session.current_iteration,
            hypothesis='baseline' if is_baseline else '',
            metric_name=session.metric_name,
            metric_value=None,
            baseline_value=session.baseline_metric,
            improved=False,
        )

        start = time.time()
        try:
            from integrations.coding_agent.aider_core.run_cmd import run_cmd_subprocess
            exit_code, output = run_cmd_subprocess(
                session.run_command,
                cwd=session.repo_path,
                timeout=session.time_budget_s,
            )
            result.run_output = output[-5000:] if len(output) > 5000 else output
            result.duration_s = time.time() - start

            if exit_code != 0:
                lines = output.split('\n')
                tb_start = None
                for i, line in enumerate(lines):
                    if 'Traceback' in line or 'Error' in line:
                        tb_start = i
                        break
                if tb_start is not None:
                    result.error = '\n'.join(lines[tb_start:tb_start + 20])
                else:
                    result.error = f'Exit code {exit_code}: {lines[-3:]}'
                return result

            metric_val = self.extract_metric(output, session)
            result.metric_value = metric_val

            if metric_val is not None and is_baseline:
                result.improved = False

            self.record_benchmark(session, result)

        except Exception as e:
            result.error = str(e)
            result.duration_s = time.time() - start

        return result

    def record_benchmark(self, session: AutoResearchSession,
                         result: ExperimentResult):
        """Record experiment result in BenchmarkTracker for evolution tracking."""
        try:
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            tracker.record(
                task_type='autoresearch',
                tool_name='aider_native_backend',
                completion_time_s=result.duration_s,
                success=not result.error and result.metric_value is not None,
                model_name=session.metric_name,
                user_id=session.goal_id or session.session_id,
            )
        except Exception:
            pass

    def extract_metric(self, output: str, session: AutoResearchSession
                       ) -> Optional[float]:
        """Extract the target metric from experiment output."""
        import re

        if session.metric_pattern:
            match = re.search(session.metric_pattern, output)
            if match:
                try:
                    return float(match.group(1))
                except (ValueError, IndexError):
                    pass

        patterns = [
            rf'{re.escape(session.metric_name)}[:\s=]+([0-9]+\.?[0-9]*)',
            rf'^{re.escape(session.metric_name)}[:\s]+([0-9]+\.?[0-9]*)',
            r'(\d+) passed',
            r'(?:score|result|metric|accuracy|loss|bpb)[:\s=]+([0-9]+\.?[0-9]*)',
        ]

        for pat in patterns:
            match = re.search(pat, output, re.IGNORECASE | re.MULTILINE)
            if match:
                try:
                    return float(match.group(1))
                except (ValueError, IndexError):
                    continue

        return None

    # ── Git State Management ─────────────────────────────────

    def revert_changes(self, session: AutoResearchSession):
        """Revert the working directory to the last good state."""
        try:
            from integrations.coding_agent.aider_core.run_cmd import run_cmd_subprocess
            run_cmd_subprocess(
                f'git checkout -- {session.target_file}',
                cwd=session.repo_path,
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"[{session.session_id}] Revert failed: {e}")

    def commit_improvement(self, session: AutoResearchSession,
                           result: ExperimentResult):
        """Commit the improvement to git and save as recipe step."""
        try:
            from integrations.coding_agent.aider_core.run_cmd import run_cmd_subprocess
            msg = (f"autoresearch iter {result.iteration}: "
                   f"{session.metric_name}={result.metric_value} "
                   f"(was {result.baseline_value})")
            run_cmd_subprocess(
                f'git add {session.target_file} && git commit -m "{msg}"',
                cwd=session.repo_path,
                timeout=15,
            )
        except Exception as e:
            logger.debug(f"[{session.session_id}] Git commit skipped: {e}")

        try:
            from integrations.coding_agent.recipe_bridge import CodingRecipeBridge
            bridge = CodingRecipeBridge()
            bridge.capture_edit_as_recipe_step(result.edits)
        except Exception:
            pass

        try:
            from integrations.agent_engine.agent_baseline_service import AgentBaselineService
            AgentBaselineService.capture_snapshot(
                prompt_id=session.experiment_id or session.session_id,
                flow_id='autoresearch',
                trigger='autoresearch_improvement',
                user_id=session.goal_id or 'system',
            )
        except Exception:
            pass

    # ── History & Reporting ──────────────────────────────────

    def build_history_summary(self, session: AutoResearchSession) -> str:
        """Build a compact summary of previous iterations for the LLM."""
        if not session.results:
            return 'No previous iterations.'

        lines = []
        for r in session.results[-10:]:
            status = 'IMPROVED' if r.get('improved') else 'reverted'
            val = r.get('metric_value', '?')
            hyp = r.get('hypothesis', '')[:80]
            err = r.get('error', '')[:50]
            if err:
                lines.append(f"  iter {r.get('iteration', '?')}: CRASHED — {err}")
            else:
                lines.append(f"  iter {r.get('iteration', '?')}: {val} ({status}) — {hyp}")

        return '\n'.join(lines)

    def save_report(self, session: AutoResearchSession):
        """Save the session report to agent_data for persistence."""
        try:
            report_dir = os.path.join(
                os.path.dirname(__file__), '..', '..', 'agent_data', 'autoresearch')
            os.makedirs(report_dir, exist_ok=True)

            report_path = os.path.join(report_dir, f'{session.session_id}.json')
            report = {
                'session': session.to_progress_dict(),
                'config': {
                    'repo_path': session.repo_path,
                    'target_file': session.target_file,
                    'run_command': session.run_command,
                    'metric_name': session.metric_name,
                    'metric_direction': session.metric_direction,
                    'max_iterations': session.max_iterations,
                    'time_budget_s': session.time_budget_s,
                },
                'results': session.results,
            }
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, default=str)
            logger.info(f"[{session.session_id}] Report saved: {report_path}")
        except Exception as e:
            logger.warning(f"[{session.session_id}] Report save failed: {e}")

        self.export_learning_delta(session)

    def export_learning_delta(self, session: AutoResearchSession):
        """Export session results as a federated learning delta."""
        try:
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            delta = tracker.export_learning_delta()
            delta['autoresearch'] = {
                'session_id': session.session_id,
                'experiment_id': session.experiment_id,
                'metric_name': session.metric_name,
                'baseline': session.baseline_metric,
                'best': session.best_metric,
                'total_improvements': session.total_improvements,
                'iterations': session.current_iteration,
            }
            logger.info(f"[{session.session_id}] Learning delta prepared for federation")
        except Exception:
            pass

    def emit_progress(self, session: AutoResearchSession,
                      event_topic: str, data: Dict = None):
        """Emit progress event via EventBus for live tracker updates."""
        try:
            from core.platform.events import emit_event
            payload = data or {}
            payload['session_id'] = session.session_id
            payload['experiment_id'] = session.experiment_id
            payload['goal_id'] = session.goal_id
            emit_event(event_topic, payload)
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────

_engine: Optional[AutoResearchEngine] = None
_engine_lock = threading.Lock()


def get_autoresearch_engine() -> AutoResearchEngine:
    """Get or create the singleton AutoResearchEngine."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = AutoResearchEngine()
    return _engine


# ── Agent Tool Functions (step-based) ────────────────────────
# The agent calls these in sequence. The agent's conversation loop
# IS the iteration loop — no hardcoded Python while loop.


def autoresearch_setup(repo_path: str, target_file: str, run_command: str,
                       metric_name: str = 'score',
                       metric_pattern: str = '',
                       metric_direction: str = 'higher_is_better',
                       max_iterations: int = 50,
                       time_budget_s: int = 300,
                       experiment_id: str = '',
                       goal_id: str = '') -> str:
    """Set up an autoresearch session and run the baseline experiment.

    Call this FIRST. Creates a session, runs the unmodified code to capture
    the baseline metric, and returns a session_id for subsequent steps.

    Agent loop pattern:
        1. autoresearch_setup(...)           → get session_id + baseline
        2. autoresearch_edit(session_id)      → propose code edit
        3. autoresearch_run(session_id)       → run + score
        4. autoresearch_decide(session_id)    → keep or revert
        5. Repeat 2-4 until converged or budget exhausted
        6. autoresearch_finalize(session_id)  → save report

    Args:
        repo_path: Path to the git repository
        target_file: The file to modify (relative to repo_path)
        run_command: Shell command to run the experiment
        metric_name: Name of the metric to optimize
        metric_pattern: Regex with group(1) to extract metric from output
        metric_direction: 'higher_is_better' or 'lower_is_better'
        max_iterations: Maximum iterations before stopping
        time_budget_s: Per-iteration time budget in seconds
        experiment_id: ThoughtExperiment ID (if triggered by one)
        goal_id: AgentGoal ID

    Returns:
        JSON with session_id, baseline_metric, and status
    """
    if not os.path.isdir(repo_path):
        return json.dumps({'error': f'repo_path not found: {repo_path}'})

    target_path = os.path.join(repo_path, target_file)
    if not os.path.isfile(target_path):
        return json.dumps({'error': f'target_file not found: {target_file}'})

    session = AutoResearchSession(
        experiment_id=experiment_id,
        goal_id=goal_id,
        repo_path=repo_path,
        target_file=target_file,
        run_command=run_command,
        metric_name=metric_name,
        metric_pattern=metric_pattern,
        metric_direction=metric_direction,
        max_iterations=max_iterations,
        time_budget_s=time_budget_s,
    )
    session.status = 'running'
    session.start_time = time.time()

    engine = get_autoresearch_engine()
    engine.register_session(session)

    # Run baseline
    baseline = engine.run_experiment(session, is_baseline=True)
    if baseline.error:
        session.status = 'failed'
        session.results.append(asdict(baseline))
        engine.emit_progress(session, 'autoresearch.failed',
                             {'error': f'Baseline failed: {baseline.error}'})
        return json.dumps({
            'error': f'Baseline failed: {baseline.error}',
            'session_id': session.session_id,
        })

    session.baseline_metric = baseline.metric_value
    session.best_metric = baseline.metric_value
    session.results.append(asdict(baseline))
    engine.emit_progress(session, 'autoresearch.started')
    engine.emit_progress(session, 'autoresearch.baseline',
                         {'baseline': baseline.metric_value})

    return json.dumps({
        'session_id': session.session_id,
        'status': 'running',
        'baseline_metric': baseline.metric_value,
        'metric_name': metric_name,
        'metric_direction': metric_direction,
        'max_iterations': max_iterations,
        'instruction': (
            'Baseline captured. Now call autoresearch_edit to propose a code '
            'change, then autoresearch_run to test it, then autoresearch_decide '
            'to keep or revert. Repeat until converged or budget exhausted.'
        ),
    })


def autoresearch_edit(session_id: str) -> str:
    """Propose and apply one code edit for an autoresearch session.

    Uses LLM + AiderNativeBackend to generate a hypothesis and apply
    the code modification. Call autoresearch_run next to test it.

    Args:
        session_id: The session ID from autoresearch_setup

    Returns:
        JSON with hypothesis, files_changed, and budget status
    """
    engine = get_autoresearch_engine()
    session = engine.get_session(session_id)
    if not session:
        return json.dumps({'error': f'Session {session_id} not found'})

    if session.is_budget_exhausted():
        session.status = 'budget_exhausted'
        return json.dumps({
            'budget_exhausted': True,
            'spark_consumed': session.spark_consumed,
            'spark_budget': session.spark_budget,
            'instruction': 'Budget exhausted. Call autoresearch_finalize to save report.',
        })

    session.current_iteration += 1
    edit_result = engine.generate_and_apply_edit(session)

    if not edit_result:
        return json.dumps({
            'success': False,
            'iteration': session.current_iteration,
            'reason': 'No edit generated by LLM',
            'instruction': 'Try calling autoresearch_edit again for a new hypothesis.',
        })

    hypothesis, edits, files_changed = edit_result
    session._pending_hypothesis = hypothesis
    session._pending_edits = edits
    session._pending_files = files_changed

    return json.dumps({
        'success': True,
        'iteration': session.current_iteration,
        'hypothesis': hypothesis,
        'files_changed': files_changed,
        'instruction': 'Edit applied. Call autoresearch_run to test this change.',
    })


def autoresearch_run(session_id: str) -> str:
    """Run the experiment after an edit and extract the metric.

    Executes the run_command, extracts the target metric from output,
    and records the result in BenchmarkTracker.

    Args:
        session_id: The session ID from autoresearch_setup

    Returns:
        JSON with metric_value, improved, and comparison to best
    """
    engine = get_autoresearch_engine()
    session = engine.get_session(session_id)
    if not session:
        return json.dumps({'error': f'Session {session_id} not found'})

    result = engine.run_experiment(session, is_baseline=False)
    result.iteration = session.current_iteration
    result.hypothesis = session._pending_hypothesis
    result.edits = session._pending_edits
    result.files_changed = session._pending_files

    session.spark_consumed += session.spark_per_iteration

    # Determine improvement
    improved = False
    if result.error:
        improved = False
    elif result.metric_value is not None and session.is_improved(result.metric_value):
        improved = True

    result.improved = improved
    result.baseline_value = session.best_metric

    # Store for decide step
    session.results.append(asdict(result))
    engine.emit_progress(session, 'autoresearch.iteration', asdict(result))

    return json.dumps({
        'iteration': session.current_iteration,
        'metric_value': result.metric_value,
        'best_metric': session.best_metric,
        'improved': improved,
        'error': result.error or None,
        'duration_s': round(result.duration_s, 1),
        'instruction': (
            f'{"IMPROVED" if improved else "No improvement"}. '
            f'Call autoresearch_decide to {"keep" if improved else "revert"} this change.'
        ),
    })


def autoresearch_decide(session_id: str) -> str:
    """Keep or revert the last edit based on the experiment result.

    If the last run improved the metric, commits the change and saves
    it as a recipe step. If not, reverts via git checkout.

    Args:
        session_id: The session ID from autoresearch_setup

    Returns:
        JSON with decision, current best, and next step advice
    """
    engine = get_autoresearch_engine()
    session = engine.get_session(session_id)
    if not session:
        return json.dumps({'error': f'Session {session_id} not found'})

    if not session.results:
        return json.dumps({'error': 'No experiment results to decide on'})

    last_result_dict = session.results[-1]
    improved = last_result_dict.get('improved', False)
    metric_value = last_result_dict.get('metric_value')
    error = last_result_dict.get('error', '')

    if error or not improved:
        # Revert
        engine.revert_changes(session)
        decision = 'reverted'
        logger.info(f"[{session.session_id}] Iter {session.current_iteration} "
                     f"reverted: {metric_value} vs best {session.best_metric}")
    else:
        # Keep — commit + recipe + baseline snapshot
        result = ExperimentResult(
            iteration=session.current_iteration,
            hypothesis=session._pending_hypothesis,
            metric_name=session.metric_name,
            metric_value=metric_value,
            baseline_value=session.best_metric,
            improved=True,
            edits=session._pending_edits,
            files_changed=session._pending_files,
        )
        session.best_metric = metric_value
        session.best_iteration = session.current_iteration
        session.total_improvements += 1
        engine.commit_improvement(session, result)
        decision = 'kept'
        logger.info(f"[{session.session_id}] Iter {session.current_iteration} "
                     f"IMPROVED: {metric_value} (was {result.baseline_value})")

    # Clear pending state
    session._pending_hypothesis = ''
    session._pending_edits = []
    session._pending_files = []

    # Convergence check
    should_continue = (
        session.current_iteration < session.max_iterations
        and not session.is_budget_exhausted()
    )

    return json.dumps({
        'decision': decision,
        'iteration': session.current_iteration,
        'best_metric': session.best_metric,
        'best_iteration': session.best_iteration,
        'total_improvements': session.total_improvements,
        'spark_consumed': session.spark_consumed,
        'should_continue': should_continue,
        'instruction': (
            'Call autoresearch_edit for the next iteration.'
            if should_continue else
            'Done iterating. Call autoresearch_finalize to save the report.'
        ),
    })


def autoresearch_finalize(session_id: str) -> str:
    """Finalize an autoresearch session — save report and export deltas.

    Call this when iteration is complete (converged, budget exhausted,
    or max iterations reached). Saves the session report and exports
    learning deltas for hive-wide federation.

    Args:
        session_id: The session ID from autoresearch_setup

    Returns:
        JSON with final session summary
    """
    engine = get_autoresearch_engine()
    session = engine.get_session(session_id)
    if not session:
        return json.dumps({'error': f'Session {session_id} not found'})

    if session.status == 'running':
        session.status = 'completed'

    engine.save_report(session)
    engine.emit_progress(session, 'autoresearch.completed',
                         session.to_progress_dict())
    engine.unregister_session(session_id)

    return json.dumps({
        'status': session.status,
        'session_id': session.session_id,
        'baseline_metric': session.baseline_metric,
        'best_metric': session.best_metric,
        'best_iteration': session.best_iteration,
        'total_improvements': session.total_improvements,
        'total_iterations': session.current_iteration,
        'spark_consumed': session.spark_consumed,
        'elapsed_s': round(time.time() - session.start_time, 1),
    })


def get_autoresearch_status(session_id: str = '') -> str:
    """Get the status of an autoresearch session or all active sessions.

    Args:
        session_id: Specific session ID, or empty for all active sessions

    Returns:
        JSON with session progress
    """
    engine = get_autoresearch_engine()

    if session_id:
        session = engine.get_session(session_id)
        if session:
            return json.dumps(session.to_progress_dict())
        # Check saved reports
        report_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'agent_data',
            'autoresearch', f'{session_id}.json')
        if os.path.isfile(report_path):
            with open(report_path, 'r') as f:
                return f.read()
        return json.dumps({'error': f'Session {session_id} not found'})

    return json.dumps({'active_sessions': engine.get_active_sessions()})


# ── Backward-compatible alias ─────────────────────────────────
# launch_experiment_autoresearch in thought_experiment_tools.py calls this

def start_autoresearch(repo_path: str, target_file: str, run_command: str,
                       metric_name: str = 'score', metric_pattern: str = '',
                       metric_direction: str = 'higher_is_better',
                       max_iterations: int = 50, time_budget_s: int = 300,
                       experiment_id: str = '', goal_id: str = '',
                       hive_parallel: bool = False,
                       num_variants: int = 3) -> str:
    """Backward-compatible wrapper — delegates to autoresearch_setup.

    The hive_parallel parameter is accepted but ignored (hive dispatch
    is now handled by the agent via compute mesh tools).
    """
    return autoresearch_setup(
        repo_path=repo_path, target_file=target_file,
        run_command=run_command, metric_name=metric_name,
        metric_pattern=metric_pattern, metric_direction=metric_direction,
        max_iterations=max_iterations, time_budget_s=time_budget_s,
        experiment_id=experiment_id, goal_id=goal_id,
    )


# Tool registration list (consumed by ServiceToolRegistry)
AUTOEVOLVE_CODE_TOOLS = [
    {
        'name': 'autoresearch_setup',
        'func': autoresearch_setup,
        'description': (
            'Set up a code research session and run baseline. Returns session_id. '
            'Call autoresearch_edit → autoresearch_run → autoresearch_decide in a loop.'
        ),
        'tags': ['autoresearch', 'coding'],
    },
    {
        'name': 'autoresearch_edit',
        'func': autoresearch_edit,
        'description': 'Propose and apply one LLM-generated code edit.',
        'tags': ['autoresearch', 'coding'],
    },
    {
        'name': 'autoresearch_run',
        'func': autoresearch_run,
        'description': 'Run the experiment after an edit and extract the metric.',
        'tags': ['autoresearch', 'coding'],
    },
    {
        'name': 'autoresearch_decide',
        'func': autoresearch_decide,
        'description': 'Keep (git commit) or revert (git checkout) the last edit.',
        'tags': ['autoresearch', 'coding'],
    },
    {
        'name': 'autoresearch_finalize',
        'func': autoresearch_finalize,
        'description': 'Save session report and export learning deltas to federation.',
        'tags': ['autoresearch', 'coding'],
    },
    {
        'name': 'get_autoresearch_status',
        'func': get_autoresearch_status,
        'description': 'Get progress of an autoresearch session or list all active sessions.',
        'tags': ['autoresearch'],
    },
]

# Backward-compat alias
AUTORESEARCH_TOOLS = AUTOEVOLVE_CODE_TOOLS
