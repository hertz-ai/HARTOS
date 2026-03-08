"""
Coding Agent Orchestrator — Unified entry point for coding tool execution.

Singleton orchestrator that:
- Auto-detects installed tools
- Routes tasks to the best tool via CodingToolRouter
- Records benchmarks for distributed learning
- Supports compute-aware hive offload when local compute is insufficient
- Exposes list_tools() and get_benchmarks() for Nunba settings UI

This is a LEAF tool — it calls external CLI subprocesses, never /chat.
This eliminates callback loops and double-dispatch antipatterns.
"""
import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger('hevolve.coding_agent')


class CodingAgentOrchestrator:
    """Singleton coding agent orchestrator."""

    def __init__(self):
        self._lock = threading.Lock()

    def execute(self, task: str, task_type: str = 'feature',
                preferred_tool: str = '', user_id: str = '',
                model: str = '', working_dir: str = '') -> Dict:
        """Execute a coding task using the best available tool.

        This is a terminal operation — calls subprocess, never /chat.
        Safe to invoke from within an AutoGen agent's tool execution.

        Args:
            task: The coding task description
            task_type: code_review, feature, bug_fix, refactor, app_build
            preferred_tool: User override (kilocode, claude_code, opencode)
            user_id: For benchmark tracking
            model: LLM model override (empty = use tool's default)
            working_dir: Working directory for the coding tool

        Returns:
            {success, output, tool, execution_time_s, task_type, error?}
        """
        # Check compute tier — offload if local compute insufficient
        if not self._can_run_locally():
            return self._offload_to_hive(task, task_type, preferred_tool,
                                          user_id, model, working_dir)

        return self._execute_local(task, task_type, preferred_tool,
                                    user_id, model, working_dir)

    def _execute_local(self, task: str, task_type: str,
                        preferred_tool: str, user_id: str,
                        model: str, working_dir: str) -> Dict:
        """Execute locally via subprocess."""
        from .tool_router import CodingToolRouter
        from .benchmark_tracker import get_benchmark_tracker

        router = CodingToolRouter()
        backend = router.route(task, task_type, preferred_tool)

        if backend is None:
            return {
                'success': False,
                'output': '',
                'tool': 'none',
                'task_type': task_type,
                'error': 'No coding tools installed. '
                         'Install one: kilocode, claude (Claude Code), or opencode.',
            }

        # Build context for the backend
        context = {}
        if model:
            context['model'] = model
        if working_dir:
            context['working_dir'] = working_dir

        # Execute via subprocess (leaf operation, no /chat re-entry)
        result = backend.execute(task, context)
        result['task_type'] = task_type

        # Record benchmark
        tracker = get_benchmark_tracker()
        tracker.record(
            task_type=task_type,
            tool_name=result.get('tool', backend.name),
            completion_time_s=result.get('execution_time_s', 0),
            success=result.get('success', False),
            model_name=model,
            user_id=user_id,
        )

        return result

    def _can_run_locally(self) -> bool:
        """Check if this node has sufficient compute for coding tools.

        Coding tools are CLI subprocesses — they mostly need network
        (for API calls) and disk space, not GPU. So the gate is lenient.
        """
        try:
            from security.system_requirements import (
                get_tier, _TIER_RANK, NodeTierLevel, FEATURE_TIER_MAP
            )
            feature_entry = FEATURE_TIER_MAP.get('coding_aggregator')
            if not feature_entry:
                return True  # Feature not in map yet, allow
            min_tier, _ = feature_entry
            current = get_tier()
            return _TIER_RANK[current] >= _TIER_RANK[min_tier]
        except Exception:
            return True  # If system_requirements unavailable, allow

    def _offload_to_hive(self, task: str, task_type: str,
                          preferred_tool: str, user_id: str,
                          model: str, working_dir: str) -> Dict:
        """Offload to a hive peer with sufficient compute.

        Uses E2E encryption via channel_encryption.py (X25519 + AES-256-GCM).
        """
        try:
            from integrations.agent_engine.compute_mesh_service import get_compute_mesh
            from security.channel_encryption import (
                encrypt_json_for_peer, decrypt_json_from_peer
            )
            from core.http_pool import pooled_post

            mesh = get_compute_mesh()
            peers = mesh.get_available_peers()

            if not peers:
                # No peers available, try locally anyway
                logger.info("No hive peers available, attempting local execution")
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # Pick best peer
            best_peer = max(peers, key=lambda p: mesh.score(p))
            peer_pub = best_peer.get('x25519_public_hex')
            peer_url = best_peer.get('url', '')

            if not peer_pub or not peer_url:
                return self._execute_local(task, task_type, preferred_tool,
                                            user_id, model, working_dir)

            # Encrypt payload (forward secrecy via ephemeral keys)
            payload = {
                'task': task,
                'task_type': task_type,
                'preferred_tool': preferred_tool,
                'model': model,
            }
            envelope = encrypt_json_for_peer(payload, peer_pub)

            # POST to peer's /coding/execute endpoint
            resp = pooled_post(
                f'{peer_url.rstrip("/")}/coding/execute',
                json={'encrypted': envelope},
                timeout=300,
            )

            if resp.status_code == 200:
                encrypted_result = resp.json().get('encrypted')
                if encrypted_result:
                    result = decrypt_json_from_peer(encrypted_result)
                    if result:
                        result['offloaded'] = True
                        # Record benchmark as offloaded
                        from .benchmark_tracker import get_benchmark_tracker
                        tracker = get_benchmark_tracker()
                        tracker.record(
                            task_type=task_type,
                            tool_name=result.get('tool', 'unknown'),
                            completion_time_s=result.get('execution_time_s', 0),
                            success=result.get('success', False),
                            model_name=model,
                            user_id=user_id,
                            offloaded=True,
                        )
                        return result

        except Exception as e:
            logger.warning(f"Hive offload failed ({e}), falling back to local")

        # Fallback to local
        return self._execute_local(task, task_type, preferred_tool,
                                    user_id, model, working_dir)

    def list_tools(self) -> Dict:
        """List all tools with install status, capabilities, and benchmarks."""
        from .installer import get_tool_info
        from .benchmark_tracker import get_benchmark_tracker

        tools = get_tool_info()
        try:
            benchmarks = get_benchmark_tracker().get_summary()
        except Exception:
            benchmarks = {'total_benchmarks': 0, 'by_tool': [], 'by_task_type': []}

        return {
            'tools': tools,
            'benchmarks': benchmarks,
            'can_run_locally': self._can_run_locally(),
        }

    def get_benchmarks(self) -> Dict:
        """Get benchmark dashboard data."""
        from .benchmark_tracker import get_benchmark_tracker
        return get_benchmark_tracker().get_summary()


# ─── Module-level singleton ───
_orchestrator = None
_orchestrator_lock = threading.Lock()


def get_coding_orchestrator() -> CodingAgentOrchestrator:
    """Get or create the singleton CodingAgentOrchestrator."""
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                _orchestrator = CodingAgentOrchestrator()
    return _orchestrator
