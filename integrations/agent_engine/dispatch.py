"""
Unified Agent Goal Engine - Chat Dispatch

Sends agent goals to idle agents via the existing /chat endpoint
(CREATE/REUSE pipeline). Dispatches with autonomous=True so the
LLM auto-generates the agent config without user interaction.

First dispatch = CREATE mode (gather_info + recipe creation).
Subsequent dispatches = REUSE mode (recipe exists, 90% faster).

DISTRIBUTED DISPATCH (automatic):
When a shared Redis coordinator is reachable (i.e. the node is part
of a hive with peers), goals are automatically submitted to the
DistributedTaskCoordinator instead of local /chat. Worker nodes
across the hive claim and execute tasks autonomously. No separate
mode flag — distribution is an emergent property of having peers.
Falls back to local /chat when Redis is unavailable.
"""
import os
import logging
import threading
import requests
from typing import Dict, List, Optional

from core.http_pool import pooled_post
from core.port_registry import get_port

logger = logging.getLogger('hevolve_social')

# ── LLM concurrency control ──────────────────────────────────────────────
# Local llama-server degrades exponentially with concurrent requests
# (KV cache thrashing). Allow only N concurrent local LLM calls.
# This prevents the watchdog-restart cascade where restarted daemons
# pile up concurrent requests that each take longer, triggering more
# restarts.
_LOCAL_LLM_MAX_CONCURRENT = int(os.environ.get('HEVOLVE_LOCAL_LLM_MAX_CONCURRENT', '1'))
_local_llm_semaphore = threading.Semaphore(_LOCAL_LLM_MAX_CONCURRENT)


# ── User-priority gate ──────────────────────────────────────────────────
# When a human user is chatting, daemon dispatch must yield the LLM.
# Tracked via timestamp of last user activity — daemon checks freshness.
import time as _time
_last_user_chat_at: float = 0.0
_USER_CHAT_COOLDOWN = 600  # 10 min — CREATE pipeline can take this long
_active_create_sessions: int = 0  # count of in-flight CREATE requests
_create_lock = threading.Lock()


def mark_user_chat_activity():
    """Call on every user /chat request (including autonomous CREATE)."""
    global _last_user_chat_at
    _last_user_chat_at = _time.time()


def mark_create_start():
    """Call when a CREATE pipeline starts."""
    global _active_create_sessions
    with _create_lock:
        _active_create_sessions += 1
    mark_user_chat_activity()


def mark_create_end():
    """Call when a CREATE pipeline finishes."""
    global _active_create_sessions
    with _create_lock:
        _active_create_sessions = max(0, _active_create_sessions - 1)


def is_user_recently_active() -> bool:
    """True if user chatted recently OR a CREATE pipeline is running."""
    if _active_create_sessions > 0:
        return True
    return (_time.time() - _last_user_chat_at) < _USER_CHAT_COOLDOWN


def _notify_watchdog_llm_start():
    """Tell the watchdog the current thread is blocked on a legitimate LLM call.

    The watchdog will extend the heartbeat threshold for threads marked
    'in_llm_call' instead of restarting them.
    """
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if not wd:
            return
        thread_name = threading.current_thread().name
        # Match by thread name — works for all daemon threads
        if wd.is_registered(thread_name):
            wd.mark_in_llm_call(thread_name)
            return
        # Partial match (thread name might have suffix like 'agent_daemon-1')
        for name in wd.registered_names():
            if name in thread_name:
                wd.mark_in_llm_call(name)
                return
        # Fallback: in-process/bundled mode — dispatch runs on a
        # different thread (e.g. Flask worker). Mark the calling daemon
        # via threadlocal source hint if available.
        try:
            from threadlocal import get_task_source
            source = get_task_source()
            if source and wd.is_registered(source):
                wd.mark_in_llm_call(source)
                return
        except Exception:
            pass
    except Exception:
        pass


def _notify_watchdog_llm_end():
    """Clear the LLM call marker and send a heartbeat for all registered daemons."""
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if wd:
            for name in wd.registered_names():
                wd.clear_llm_call(name)
                wd.heartbeat(name)
    except Exception:
        pass


def _get_distributed_coordinator():
    """Get the shared DistributedTaskCoordinator if Redis is reachable.

    Returns None when Redis is unavailable — caller falls back to local.
    No separate mode flag needed: if Redis exists, distribute.
    """
    try:
        from integrations.distributed_agent.api import _get_coordinator
        return _get_coordinator()
    except Exception as e:
        logger.debug(f"Distributed coordinator unavailable: {e}")
        return None


def _has_hive_peers() -> bool:
    """Check if this node has active peers in the hive.

    Distribution only makes sense when there are other nodes to
    pick up work. Single-node setups always dispatch locally.
    """
    try:
        from integrations.social.models import db_session, PeerNode
        with db_session(commit=False) as db:
            count = db.query(PeerNode).filter(
                PeerNode.status == 'active'
            ).count()
            return count > 1  # >1 because self is in the table too
    except Exception:
        return False


def _decompose_goal(prompt: str, goal_id: str, goal_type: str,
                    user_id: str) -> List[Dict]:
    """Decompose a goal into distributable sub-tasks.

    Checks AgentGoal.context for explicit subtask definitions:
        {"tasks": [...], "parallel": true/false}

    When subtasks are present, uses SmartLedger to create a proper
    dependency graph (parallel fan-out or sequential chain).
    Falls back to single-task decomposition when no subtasks defined.
    """
    try:
        from .parallel_dispatch import (
            extract_subtasks_from_context, decompose_goal_to_ledger)

        subtask_defs = extract_subtasks_from_context(goal_id)
        tasks, _ledger = decompose_goal_to_ledger(
            prompt, goal_id, goal_type, user_id, subtask_defs)
        return tasks
    except Exception:
        pass

    return [{
        'task_id': f'{goal_id}_task_0',
        'description': prompt[:500],
        'capabilities': [goal_type],
    }]


def dispatch_goal_distributed(prompt: str, user_id: str, goal_id: str,
                              goal_type: str = 'marketing') -> Optional[str]:
    """Submit a goal to the distributed task coordinator.

    The goal is decomposed into sub-tasks, published to shared Redis,
    and worker nodes across the hive will claim and execute them.

    Returns:
        goal_id string on success, None on failure
    """
    coordinator = _get_distributed_coordinator()
    if not coordinator:
        logger.warning(f"Distributed dispatch failed: coordinator unavailable, "
                       f"falling back to local for {goal_type} goal {goal_id}")
        return None

    tasks = _decompose_goal(prompt, goal_id, goal_type, user_id)
    context = {
        'goal_type': goal_type,
        'user_id': user_id,
        'prompt': prompt,
        'source_node': os.environ.get('HEVOLVE_NODE_ID', 'unknown'),
        'task_source': 'hive',
    }

    try:
        distributed_goal_id = coordinator.submit_goal(
            objective=prompt[:200],
            decomposed_tasks=tasks,
            context=context,
        )
        logger.info(f"Distributed dispatch: goal {goal_id} submitted as "
                    f"{distributed_goal_id} with {len(tasks)} tasks")
        return distributed_goal_id
    except Exception as e:
        logger.warning(f"Distributed dispatch error for {goal_type} goal {goal_id}: {e}")
        return None


def _check_robot_capability_match(goal_type: str, goal_id: str) -> bool:
    """For robot goals, verify this node can handle the task.

    Checks task requirements against local robot capabilities.
    Non-robot goals always pass.  Robot goals without requirements pass.

    Returns True if the node is capable, False if it should be
    dispatched to a more capable peer via distributed dispatch.
    """
    if goal_type != 'robot':
        return True

    try:
        from integrations.social.models import db_session, AgentGoal
        with db_session(commit=False) as db:
            goal = db.query(AgentGoal).filter_by(id=goal_id).first()
            if not goal:
                return True
            config = goal.config_json or {}
            required_caps = config.get('required_capabilities', [])
            if not required_caps:
                return True

            from integrations.robotics.capability_advertiser import (
                get_capability_advertiser,
            )
            adv = get_capability_advertiser()
            score = adv.matches_task_requirements({
                'required_capabilities': required_caps,
                'preferred_form_factor': config.get('preferred_form_factor'),
                'min_payload_kg': config.get('min_payload_kg'),
            })
            if score < 0.5:
                logger.info(
                    f"Robot goal {goal_id} capability mismatch "
                    f"(score={score}), prefer distributed dispatch")
                return False
            return True
    except Exception as e:
        logger.debug(f"Robot capability check skipped: {e}")
        return True


def dispatch_goal(prompt: str, user_id: str, goal_id: str,
                  goal_type: str = 'marketing',
                  model_config: list = None) -> Optional[str]:
    """Send a goal prompt through the existing /chat pipeline.

    Uses autonomous=True so Phase 1 (gather_info) runs without
    human interaction — the LLM generates the agent config itself.

    GUARDRAILS enforced: GuardrailEnforcer.before_dispatch() + after_response().

    When Redis is reachable and hive peers exist, goals are automatically
    submitted to the shared DistributedTaskCoordinator. Worker nodes
    across the hive claim and execute them. Falls back to local /chat
    when the coordinator is unavailable or no peers exist.

    For robot goals: capability matching ensures the task goes to a
    node with the right hardware (locomotion, manipulation, sensors).

    Args:
        prompt: The goal prompt (from build_prompt)
        user_id: The agent's user_id
        goal_id: The goal identifier
        goal_type: Goal type prefix for prompt_id
        model_config: Optional per-dispatch config_list override

    Returns:
        Response text or None on failure
    """
    # BUDGET GATE: check goal budget + platform affordability before dispatch
    try:
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        bg_allowed, bg_reason = pre_dispatch_budget_gate(goal_id, prompt)
        if not bg_allowed:
            logger.warning(f"Dispatch blocked by budget gate for {goal_type} goal {goal_id}: {bg_reason}")
            return None
    except ImportError:
        pass

    # TOOL ALLOWLIST: resolve model tier and attach to dispatch context.
    # Tier is sent to /chat as body['model_tier']; create_recipe uses it
    # to call filter_tools_for_model() when building the agent tool list.
    _dispatch_model_tier = None
    if model_config:
        try:
            from integrations.agent_engine.model_registry import model_registry
            first_model = model_config[0].get('model', '') if model_config else ''
            if first_model:
                info = model_registry.get(first_model)
                if info:
                    _dispatch_model_tier = (info.get('tier') or info.get('model_tier'))
                    if _dispatch_model_tier:
                        logger.info(f"Dispatch model tier: {_dispatch_model_tier.value} "
                                    f"for {goal_type} goal {goal_id}")
        except Exception:
            pass  # Model registry unavailable — no tier restriction

    # GUARDRAIL: full pre-dispatch gate (fail-closed: block if guardrails unavailable)
    try:
        from security.hive_guardrails import GuardrailEnforcer
        allowed, reason, prompt = GuardrailEnforcer.before_dispatch(prompt)
        if not allowed:
            logger.warning(f"Dispatch blocked for {goal_type} goal {goal_id}: {reason}")
            return None
    except ImportError:
        logger.error("CRITICAL: hive_guardrails not available — blocking dispatch")
        return None

    # AUDIT LOG: record goal dispatch
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'goal_dispatched', actor_id=user_id,
            action=f'dispatch {goal_type} goal {goal_id}',
            target_id=goal_id)
    except Exception:
        pass  # Audit is best-effort

    # ROBOT: capability-matched dispatch — prefer distributed for hardware mismatches
    _tried_distributed = False
    if not _check_robot_capability_match(goal_type, goal_id):
        coordinator = _get_distributed_coordinator()
        if coordinator and _has_hive_peers():
            _tried_distributed = True
            result = dispatch_goal_distributed(prompt, user_id, goal_id, goal_type)
            if result is not None:
                return result
        # Fall through to local if no capable peer found

    # DISTRIBUTED: auto-distribute when coordinator is reachable and hive has peers
    # Skip if robot dispatch already tried distributed (avoid double submission)
    if not _tried_distributed:
        coordinator = _get_distributed_coordinator()
        if coordinator and _has_hive_peers():
            result = dispatch_goal_distributed(prompt, user_id, goal_id, goal_type)
            if result is not None:
                return result
            # Fall through to local dispatch if distributed fails
            logger.info(f"Distributed fallback -> local dispatch for {goal_type} goal {goal_id}")

    # Generate a NUMERIC prompt_id (same format as hart_intelligence_entry._next_prompt_id)
    # so it passes the isdigit() check in the adapter and /chat handler.
    # Use goal_id hash to ensure the SAME goal always gets the SAME prompt_id
    # across dispatches — this is what enables recipe reuse on subsequent ticks.
    import hashlib
    _goal_hash = int(hashlib.md5(goal_id.encode()).hexdigest()[:10], 16) % 100_000_000_000
    prompt_id = str(max(1, _goal_hash))

    body = {
        'user_id': user_id,
        'prompt_id': prompt_id,
        'prompt': prompt,
        'create_agent': True,
        'autonomous': True,
        'casual_conv': False,
        'task_source': 'own',
    }
    if model_config:
        body['model_config'] = model_config
    if _dispatch_model_tier:
        body['model_tier'] = _dispatch_model_tier.value

    # 3-tier dispatch (same as hartos_backend_adapter.py):
    #   Tier 1: Direct in-process import (no ports, no HTTP)
    #   Tier 2: HTTP proxy to backend port
    #   Tier 3: llama.cpp fallback (direct LLM, no agent pipeline)
    resp = None

    # Tier 1: Direct in-process import of hart_intelligence
    # Guarded by semaphore to prevent concurrent request pile-up on
    # local llama-server (causes exponential slowdown + watchdog restarts).
    try:
        try:
            from routes.hartos_backend_adapter import chat as hevolve_chat
        except ImportError:
            from hartos_backend_adapter import chat as hevolve_chat

        # USER PRIORITY: if user chatted recently, skip this tick — let user have the LLM
        if is_user_recently_active():
            logger.info(f"User active ({_USER_CHAT_COOLDOWN}s cooldown), deferring dispatch for goal {goal_id}")
            return None

        # Try to acquire semaphore (non-blocking check first)
        if not _local_llm_semaphore.acquire(timeout=5):
            logger.info(f"LLM busy ({_LOCAL_LLM_MAX_CONCURRENT} in flight), "
                        f"skipping dispatch for goal {goal_id}")
            return None

        # Signal to watchdog that this thread is in a legitimate LLM call
        _notify_watchdog_llm_start()
        try:
            # Use a daemon-specific request_id so thinking traces from daemon
            # dispatch are isolated from user chat traces. Without this, daemon
            # traces leak into user responses via drain_thinking_traces().
            _daemon_request_id = f'daemon_{goal_id}'
            result = hevolve_chat(
                text=prompt, user_id=user_id,
                agent_id=prompt_id, create_agent=True, casual_conv=False,
                autonomous=True, request_id=_daemon_request_id,
            )
        finally:
            _local_llm_semaphore.release()
            _notify_watchdog_llm_end()

        response = result.get('text') or result.get('response', '')
        if response:
            return response
    except ImportError:
        pass  # Nunba adapter not available — fall through to Tier 2
    except Exception as e:
        logger.warning(f"Tier-1 dispatch failed for {goal_type} goal {goal_id}: {e}")

    # Tier 2: HTTP proxy to HARTOS backend port
    base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
    resp = pooled_post(f'{base_url}/chat', json=body, timeout=120)

    try:
        if resp.status_code == 200:
            result = resp.get_json() if hasattr(resp, 'get_json') else resp.json()
            response = result.get('response', '')

            # GUARDRAIL: post-response check (fail-closed)
            try:
                from security.hive_guardrails import GuardrailEnforcer
                passed, reason = GuardrailEnforcer.after_response(response)
                if not passed:
                    logger.warning(f"Response filtered for goal {goal_id}: {reason}")
                    return None
            except ImportError:
                logger.error("CRITICAL: hive_guardrails not available — blocking response")
                return None

            # GUARDRAIL: coding goals — no merge without constitutional review
            if goal_type == 'coding':
                try:
                    from security.hive_guardrails import ConstitutionalFilter
                    review_dict = {
                        'title': f'Code commit review: {goal_id}',
                        'description': response[:2000],
                        'goal_type': 'coding',
                    }
                    passed, reason = ConstitutionalFilter.check_goal(review_dict)
                    if not passed:
                        logger.warning(
                            f"Coding goal {goal_id} output blocked by "
                            f"constitutional review: {reason}")
                        return None
                except ImportError:
                    logger.error("CRITICAL: ConstitutionalFilter not available — blocking coding goal")
                    return None

            # Record to world model (training data for hive intelligence)
            try:
                from .world_model_bridge import get_world_model_bridge
                bridge = get_world_model_bridge()
                bridge.record_interaction(
                    user_id=user_id,
                    prompt_id=prompt_id,
                    prompt=prompt,
                    response=response,
                    goal_id=goal_id,
                )
            except Exception:
                pass

            return response
        else:
            # Non-200 response — log and queue transient errors for retry
            logger.warning(
                f"Goal dispatch got HTTP {resp.status_code} for {goal_type} "
                f"goal {goal_id}: {resp.text[:200]}")
            if resp.status_code in (429, 500, 502, 503):
                try:
                    from .instruction_queue import enqueue_instruction
                    enqueue_instruction(
                        user_id=user_id, text=prompt[:2000], priority=3,
                        tags=[goal_type],
                        context={'goal_id': goal_id, 'goal_type': goal_type,
                                 'queued_reason': f'http_{resp.status_code}'},
                        related_goal_id=goal_id,
                    )
                except Exception:
                    pass
    except requests.RequestException as e:
        logger.warning(f"Goal dispatch failed for {goal_type} goal {goal_id}: {e}")

        # Queue the instruction for later execution when compute becomes available
        try:
            from .instruction_queue import enqueue_instruction
            enqueue_instruction(
                user_id=user_id,
                text=prompt[:2000],
                priority=3,
                tags=[goal_type],
                context={
                    'goal_id': goal_id,
                    'goal_type': goal_type,
                    'queued_reason': f'dispatch_failed: {e}',
                },
                related_goal_id=goal_id,
            )
            logger.info(f"Instruction queued for later: {goal_type} goal {goal_id}")
        except Exception as eq:
            logger.debug(f"Instruction queue unavailable: {eq}")

    return None


def _dispatch_single_instruction(base_url: str, user_id: str, inst,
                                  batch_id: str) -> tuple:
    """Dispatch one instruction via /chat. Returns (instruction_id, response_text, error)."""
    body = {
        'user_id': user_id,
        'prompt_id': f'iq_{batch_id}_{inst.id[:8]}',
        'prompt': inst.text,
        'create_agent': True,
        'autonomous': True,
        'casual_conv': False,
        'task_source': 'own',
    }
    try:
        resp = pooled_post(f'{base_url}/chat', json=body, timeout=300)
        if resp.status_code == 200:
            result_text = resp.json().get('response', '')
            return (inst.id, result_text[:500], None)
        return (inst.id, None, f'HTTP {resp.status_code}')
    except requests.RequestException as e:
        return (inst.id, None, str(e))


def drain_instruction_queue(user_id: str, max_tokens: int = 8000) -> Optional[str]:
    """Pull and execute queued instructions with dependency-aware dispatch.

    Uses SmartLedger's dependency graph to determine execution order:
    - Independent instructions dispatch in parallel (concurrent threads)
    - Dependent instructions wait for prerequisites to complete first

    Execution proceeds in waves:
      Wave 0: all instructions with no dependencies → parallel dispatch
      Wave 1: instructions depending on wave 0 → parallel dispatch
      ...until all waves complete.

    Falls back to single-batch dispatch when SmartLedger is unavailable.

    Called by agent_daemon.py on idle tick, or manually via API.

    Args:
        user_id: User whose queue to drain
        max_tokens: Max tokens across all instructions

    Returns:
        Combined response text, or None if queue empty or all failed
    """
    try:
        from .instruction_queue import get_queue
        q = get_queue(user_id)

        # Acquire drain lock — prevents concurrent drains for same user
        # (daemon tick + API call + another agent all trying simultaneously)
        if not q.acquire_drain_lock():
            logger.info(f"Drain skipped for {user_id}: another drain in progress")
            return None

        try:
            # Try dependency-aware execution plan
            plan = q.pull_execution_plan(max_tokens=max_tokens)
            if plan is None:
                return None

            base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
            all_results = []
            any_success = False

            logger.info(
                f"Draining instruction queue for {user_id}: "
                f"{plan.total_instructions} instructions in "
                f"{len(plan.waves)} waves"
            )

            for wave_idx, wave in enumerate(plan.waves):
                logger.info(
                    f"Wave {wave_idx + 1}/{len(plan.waves)}: "
                    f"{len(wave)} instruction(s)"
                )

                if len(wave) == 1:
                    # Single instruction — dispatch directly (no thread pool overhead)
                    inst = wave[0]
                    iid, result, error = _dispatch_single_instruction(
                        base_url, user_id, inst, plan.batch_id,
                    )
                    if error:
                        q.fail_instruction(iid, error)
                        logger.warning(f"Instruction [{iid}] failed: {error}")
                    else:
                        q.complete_instruction(iid, result)
                        all_results.append(result)
                        any_success = True
                else:
                    # Multiple independent instructions — dispatch in parallel.
                    #
                    # Thread safety:
                    # - _dispatch_single_instruction() is a pure HTTP call (no shared state)
                    # - Results collected via as_completed() on the CALLING thread
                    # - q.complete/fail_instruction() acquires q._lock (serialized)
                    # - SmartLedger mutations happen inside q._lock (no separate lock needed)
                    # - File I/O uses atomic write (temp + rename)
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(len(wave), 4),
                    ) as executor:
                        futures = {
                            executor.submit(
                                _dispatch_single_instruction,
                                base_url, user_id, inst, plan.batch_id,
                            ): inst
                            for inst in wave
                        }
                        for future in concurrent.futures.as_completed(futures):
                            iid, result, error = future.result()
                            if error:
                                q.fail_instruction(iid, error)
                                logger.warning(f"Instruction [{iid}] failed: {error}")
                            else:
                                q.complete_instruction(iid, result)
                                all_results.append(result)
                                any_success = True

            if any_success:
                combined = '\n---\n'.join(all_results)
                logger.info(
                    f"Plan {plan.batch_id} completed: "
                    f"{len(all_results)}/{plan.total_instructions} succeeded"
                )
                return combined
            return None
        finally:
            q.release_drain_lock()

    except Exception as e:
        logger.error(f"Queue drain error: {e}")
        return None
