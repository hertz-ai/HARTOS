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
import requests
from typing import Dict, List, Optional

from core.http_pool import pooled_post
from core.port_registry import get_port

logger = logging.getLogger('hevolve_social')


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

    # In bundled/desktop mode, use the in-process adapter instead of HTTP
    # to port 6777 (which doesn't run as a separate server in bundled mode).
    if os.environ.get('NUNBA_BUNDLED'):
        try:
            try:
                from routes.hartos_backend_adapter import chat as hevolve_chat
            except ImportError:
                from hartos_backend_adapter import chat as hevolve_chat
            result = hevolve_chat(
                text=prompt,
                user_id=user_id,
                agent_id=f"{goal_type}_{goal_id[:8]}",
                create_agent=True,
                casual_conv=False,
            )
            response = result.get('text') or result.get('response', '')
            if response:
                return response
        except Exception as e:
            logger.warning(f"Bundled dispatch failed for {goal_type} goal {goal_id}: {e}")
        return None

    base_url = os.environ.get('HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}')
    prompt_id = f"{goal_type}_{goal_id[:8]}"

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

    try:
        resp = pooled_post(
            f'{base_url}/chat',
            json=body,
            timeout=120,
        )
        if resp.status_code == 200:
            result = resp.json()
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
