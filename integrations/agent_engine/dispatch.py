"""
Unified Agent Goal Engine - Chat Dispatch

Sends agent goals to idle agents via the existing /chat endpoint
(CREATE/REUSE pipeline). Dispatches with autonomous=True so the
LLM auto-generates the agent config without user interaction.

First dispatch = CREATE mode (gather_info + recipe creation).
Subsequent dispatches = REUSE mode (recipe exists, 90% faster).
"""
import os
import logging
import requests
from typing import Optional

logger = logging.getLogger('hevolve_social')


def dispatch_goal(prompt: str, user_id: str, goal_id: str,
                  goal_type: str = 'marketing',
                  model_config: list = None) -> Optional[str]:
    """Send a goal prompt through the existing /chat pipeline.

    Uses autonomous=True so Phase 1 (gather_info) runs without
    human interaction — the LLM generates the agent config itself.

    GUARDRAILS enforced: GuardrailEnforcer.before_dispatch() + after_response().

    Args:
        prompt: The goal prompt (from build_prompt)
        user_id: The agent's user_id
        goal_id: The goal identifier
        goal_type: Goal type prefix for prompt_id
        model_config: Optional per-dispatch config_list override

    Returns:
        Response text or None on failure
    """
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

    base_url = os.environ.get('HEVOLVE_BASE_URL', 'http://localhost:6777')
    prompt_id = f"{goal_type}_{goal_id[:8]}"

    body = {
        'user_id': user_id,
        'prompt_id': prompt_id,
        'prompt': prompt,
        'create_agent': True,
        'autonomous': True,
        'casual_conv': False,
    }
    if model_config:
        body['model_config'] = model_config

    try:
        resp = requests.post(
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
    except requests.RequestException as e:
        logger.warning(f"Goal dispatch failed for {goal_type} goal {goal_id}: {e}")

    return None
