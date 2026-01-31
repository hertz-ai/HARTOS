"""
HevolveSocial - Task Delegation
Create tasks from social posts and delegate to agents via A2AContextExchange.
"""
import logging
from typing import Optional

logger = logging.getLogger('hevolve_social')


def delegate_to_best_agent(task_description: str, required_skill: str = None) -> Optional[str]:
    """Find the best agent for a task and return their agent_id."""
    try:
        from integrations.internal_comm.internal_agent_communication import a2a_context
        if required_skill:
            agent_id = a2a_context.skill_registry.get_best_agent_for_skill(required_skill)
            if agent_id:
                return agent_id
        # Fallback: delegate via context exchange
        result = a2a_context.delegate_task('social_requester', task_description)
        if result and 'delegated_to' in result:
            return result['delegated_to']
    except ImportError:
        logger.debug("A2AContextExchange not available for task delegation")
    except Exception as e:
        logger.debug(f"Task delegation error: {e}")
    return None


def create_ledger_task(user_id: str, prompt_id: int, task_description: str) -> Optional[str]:
    """Create a SmartLedger task for tracking. Returns ledger key."""
    try:
        from helper_ledger import create_ledger_for_user_prompt
        ledger = create_ledger_for_user_prompt(user_id, prompt_id)
        if ledger:
            task_key = f"social_task_{user_id}"
            ledger.add_task(task_key, task_description)
            return task_key
    except ImportError:
        logger.debug("SmartLedger not available for task tracking")
    except Exception as e:
        logger.debug(f"Ledger task creation error: {e}")
    return None
