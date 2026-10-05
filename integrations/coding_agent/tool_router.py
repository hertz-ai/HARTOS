"""
Coding Agent Tool Router — Intelligent routing to best coding tool.

Priority order:
    1. User override (explicit tool choice)
    2. Local benchmark data (min 5 samples per task type)
    3. Hive-aggregated intelligence (from FederatedAggregator)
    4. Heuristic defaults (claude_code rows -> COPILOT_OFF_DEFAULT while
       the copilot switch is off)
    5. COPILOT_OFF_DEFAULT while the switch is off, else first available
"""
import logging
from typing import Optional

from .tool_backends import CodingToolBackend, get_available_backends, get_all_backends

logger = logging.getLogger('hevolve.coding_agent')

#: The harness the router defaults to while the owner's copilot switch is off
#: (claude_code is then neither offered nor spawnable).
COPILOT_OFF_DEFAULT = 'hermes'

# Heuristic defaults when no benchmark data exists.
# aider_native preferred for tasks where in-process code intelligence excels.
# claw_native preferred for Rust-speed file ops, bash, grep/glob (no subprocess).
# Falls through to subprocess backends if native backends aren't available.
HEURISTIC_DEFAULTS = {
    'code_review': 'claude_code',
    'debugging': 'claude_code',
    'complex_reasoning': 'claude_code',
    'terminal_workflows': 'claw_native',      # Rust-native bash + file ops
    'terminal_coding': 'claw_native',          # Agent loop with tools
    'app_build': 'kilocode',
    'feature': 'kilocode',
    'refactor': 'aider_native',
    'bug_fix': 'aider_native',
    'multi_session': 'opencode',
    'multi_file_edit': 'aider_native',
    'architecture': 'claude_code',
    'repo_exploration': 'claw_native',         # Fast grep/glob via Rust
    'bash_execution': 'claw_native',           # Sandboxed bash via Rust
}


class CodingToolRouter:
    """Route coding tasks to the best available tool."""

    def route(self, task: str, task_type: str = 'feature',
              user_override: str = '',
              context: Optional[dict] = None) -> Optional[CodingToolBackend]:
        """Select the best backend for this task.

        Returns None if no tools are installed.
        """
        available = get_available_backends()
        if not available:
            logger.warning("No coding tools installed")
            return None

        # 1. User override — respect explicitly
        if user_override and user_override in available:
            logger.info(f"Router: user override → {user_override}")
            return available[user_override]

        # 2. Local benchmark data
        best = self._check_local_benchmarks(task_type, available)
        if best:
            logger.info(f"Router: local benchmark → {best.name}")
            return best

        # 3. Hive-aggregated intelligence
        best = self._check_hive_intelligence(task_type, available)
        if best:
            logger.info(f"Router: hive intelligence → {best.name}")
            return best

        # 4. Heuristic default.  The claude_code rows are the copilot's
        # seat: with the owner's switch off, that seat belongs to the
        # copilot-off default harness, and every other row keeps its tool.
        default_name = HEURISTIC_DEFAULTS.get(task_type, '')
        if default_name == 'claude_code':
            default_name = self._copilot_off_default(available) or default_name
        if default_name in available:
            logger.info(f"Router: heuristic → {default_name}")
            return available[default_name]

        # 5. Copilot off: the default harness; otherwise the first available.
        off_default = self._copilot_off_default(available)
        if off_default:
            logger.info(f"Router: copilot off → {off_default}")
            return available[off_default]
        first = next(iter(available.values()))
        logger.info(f"Router: fallback → {first.name}")
        return first

    @staticmethod
    def _copilot_off_default(available: dict) -> str:
        """The harness chosen when the copilot switch is off ('' when the
        switch is on or that harness is not installed)."""
        from .claude_code_backend import copilot_enabled
        if COPILOT_OFF_DEFAULT in available and not copilot_enabled():
            return COPILOT_OFF_DEFAULT
        return ''

    def _check_local_benchmarks(self, task_type: str,
                                 available: dict) -> Optional[CodingToolBackend]:
        """Check local benchmark DB for best tool."""
        try:
            from .benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            result = tracker.get_best_tool(task_type)
            if result:
                tool_name, success_rate, avg_time = result
                if tool_name in available:
                    return available[tool_name]
        except Exception:
            pass
        return None

    def _check_hive_intelligence(self, task_type: str,
                                  available: dict) -> Optional[CodingToolBackend]:
        """Check hive-aggregated routing table."""
        try:
            from .benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            tool_name = tracker.get_hive_best_tool(task_type)
            if tool_name and tool_name in available:
                return available[tool_name]
        except Exception:
            pass
        return None
