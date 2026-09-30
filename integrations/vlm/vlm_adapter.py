"""
vlm_adapter.py - Three-tier VLM execution adapter.

Follows the same pattern as Nunba's hartos_backend_adapter.py:
- Tier 1: In-process (NUNBA_BUNDLED + pyautogui + OmniParser direct import)
- Tier 2: HTTP local (flat mode, localhost:5001 screenshot/execute + localhost:8080 parse)
- Tier 3: Crossbar WAMP (central/regional mode - caller handles via subscribe_and_return)

Reuses existing env vars: NUNBA_BUNDLED, sys.frozen, HEVOLVE_NODE_TIER.
Circuit breaker: 2 consecutive failures → skip tier (same as hartos_backend_adapter.py).
"""

import os
import sys
import time
import logging
import threading

logger = logging.getLogger('hevolve.vlm_adapter')

# ---------------------------------------------------------------------------
# Reuse existing bundled-mode flag (same as hart_intelligence:329)
# ---------------------------------------------------------------------------
_BUNDLED_MODE = bool(os.environ.get('NUNBA_BUNDLED') or getattr(sys, 'frozen', False))

# ---------------------------------------------------------------------------
# Reuse existing node tier (same as create_recipe.py:285)
# ---------------------------------------------------------------------------
_node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')

# ---------------------------------------------------------------------------
# Circuit breaker (same as hartos_backend_adapter.py:49-52)
# ---------------------------------------------------------------------------
_tier1_fail_count = 0
_tier2_fail_count = 0
_FAIL_THRESHOLD = 2

# ---------------------------------------------------------------------------
# Tier 1: try direct import of pyautogui at module level
# ---------------------------------------------------------------------------
_HAS_PYAUTOGUI = False
try:
    import pyautogui  # noqa: F401
    _HAS_PYAUTOGUI = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Probe cache (avoid hammering localhost every call)
# ---------------------------------------------------------------------------
_probe_cache = {'ts': 0, 'result': None}
_PROBE_TTL = 60  # seconds


def resolve_task_workspace(prompt_id=None, explicit=None) -> str:
    """The directory a computer-use task resolves bare filenames in.

    ONE resolver for every entry point that builds a VLM message, in this
    order:

      1. an explicit workspace the caller already holds;
      2. the goal's own ``repo_path`` (AgentGoal.config_json), found by the
         run's prompt_id the way the steering endpoint finds its goal;
      3. the user-data coding workspace (core.platform_paths).

    Never the process cwd.  Measured live 2026-09-19 22:07: the loop told the
    VLM "Declared task workspace: C:\\Program Files (x86)\\HevolveAI\\Nunba",
    the frozen install's launch directory, which no task was ever assigned.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    if prompt_id not in (None, '', 0, '0'):
        try:
            from integrations.social.models import AgentGoal, get_db
            db = get_db()
            try:
                for goal in db.query(AgentGoal).filter(
                        AgentGoal.prompt_id == str(prompt_id)).all():
                    cfg = getattr(goal, 'config_json', None) or {}
                    repo = str(cfg.get('repo_path') or cfg.get('workspace_root') or '').strip()
                    if repo:
                        return repo
            finally:
                db.close()
        except Exception:
            logger.debug('workspace lookup by prompt_id unavailable', exc_info=True)
    from core.platform_paths import get_coding_workspace_dir
    return get_coding_workspace_dir()


def execute_vlm_instruction(message: dict) -> dict | None:
    """
    Three-tier VLM execution.

    Returns:
        dict  - {status, extracted_responses, execution_time_seconds} on success
        None  - signals caller to fall back to Tier 3 (Crossbar subscribe_and_return)
    """
    global _tier1_fail_count, _tier2_fail_count

    # This protects the WAMP fallback as well as the local tiers.  The same
    # policy is checked again at the final action dispatcher.
    from integrations.vlm.safety import (
        computer_operation_refusal, computer_control_block)
    refusal = computer_operation_refusal(
        message.get('instruction_to_vlm_agent') or message.get('enhanced_instruction'))
    if refusal:
        logger.warning('VLM instruction refused: %s', refusal)
        return {'status': 'blocked', 'exit_reason': 'destructive_operation',
                'extracted_responses': [{'type': 'error', 'content': refusal,
                                         'iteration': 0}]}

    consent_refusal = computer_control_block(message.get('prompt_id'))
    if consent_refusal:
        logger.warning('VLM instruction consent refused: %s', consent_refusal)
        return {'status': 'blocked', 'exit_reason': 'consent_required',
                'extracted_responses': [{'type': 'error', 'content': consent_refusal,
                                         'iteration': 0}]}

    # Tier 1: In-process (deps available + circuit breaker open)
    # Standalone HARTOS with pyautogui works — no need for NUNBA_BUNDLED gate
    if _HAS_PYAUTOGUI and _tier1_fail_count < _FAIL_THRESHOLD:
        try:
            from integrations.vlm.local_loop import run_local_agentic_loop
            result = run_local_agentic_loop(message, tier='inprocess')
            _tier1_fail_count = 0  # reset on success
            return result
        except Exception as e:
            _tier1_fail_count += 1
            logger.warning(
                f"VLM Tier 1 (in-process) failed "
                f"({_tier1_fail_count}/{_FAIL_THRESHOLD}): {e}"
            )

    # Tier 2: HTTP local (flat mode + circuit breaker open)
    if _node_tier == 'flat' and _tier2_fail_count < _FAIL_THRESHOLD:
        try:
            from integrations.vlm.local_loop import run_local_agentic_loop
            result = run_local_agentic_loop(message, tier='http')
            _tier2_fail_count = 0  # reset on success
            return result
        except Exception as e:
            _tier2_fail_count += 1
            logger.warning(
                f"VLM Tier 2 (HTTP local) failed "
                f"({_tier2_fail_count}/{_FAIL_THRESHOLD}): {e}"
            )

    # Tier 3: Crossbar WAMP - return None so caller uses subscribe_and_return()
    logger.info("VLM routing to Tier 3 (Crossbar WAMP)")
    return None


def check_vlm_available() -> bool:
    """
    Quick availability check - replaces the 2-second Crossbar ping.

    Returns True if at least one tier is expected to work.
    """
    # Tier 1: available if pyautogui present (standalone or bundled)
    if _HAS_PYAUTOGUI:
        return True

    # Tier 2: flat mode - probe local services (cached)
    if _node_tier == 'flat':
        if _probe_local_services():
            return True

    # Tier 3: Crossbar assumed available for regional/central
    # (actual connectivity checked by subscribe_and_return at call time)
    return True


def _probe_local_services() -> bool:
    """Check if OmniParser (:8080) and omnitool-gui (:5001) are reachable."""
    global _probe_cache
    now = time.time()
    if now - _probe_cache['ts'] < _PROBE_TTL and _probe_cache['result'] is not None:
        return _probe_cache['result']

    result = False
    try:
        import requests as _req
        # Quick health checks with short timeout
        omni_port = os.environ.get('OMNIPARSER_PORT', '8080')
        gui_port = os.environ.get('VLM_GUI_PORT', '5001')
        omni_ok = _req.get(f'http://localhost:{omni_port}/', timeout=2).status_code < 500
        gui_ok = _req.get(f'http://localhost:{gui_port}/', timeout=2).status_code < 500
        result = omni_ok and gui_ok
    except Exception:
        result = False

    _probe_cache['ts'] = now
    _probe_cache['result'] = result
    return result


def reset_circuit_breakers():
    """Reset all circuit breakers (useful after config change or reconnect)."""
    global _tier1_fail_count, _tier2_fail_count
    _tier1_fail_count = 0
    _tier2_fail_count = 0
    _probe_cache['ts'] = 0
    _probe_cache['result'] = None
