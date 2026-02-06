"""
HevolveSocial - Chat Dispatch

Thin layer that sends coding goals to idle agents via the existing
/chat endpoint (CREATE/REUSE pipeline). No separate task/submission
tracking — SmartLedger and ActionState handle that.
"""
import os
import logging
import requests
from typing import Optional

logger = logging.getLogger('hevolve_social')


def dispatch_to_chat(prompt: str, user_id: str, goal_id: str) -> Optional[str]:
    """Send a coding goal prompt through the existing /chat pipeline.

    This is the only integration point: the CREATE/REUSE agent system
    handles decomposition, execution, verification, and persistence.
    """
    base_url = os.environ.get('HEVOLVE_BASE_URL', 'http://localhost:6777')
    prompt_id = f"coding_{goal_id[:8]}"

    try:
        resp = requests.post(
            f'{base_url}/chat',
            json={
                'user_id': user_id,
                'prompt_id': prompt_id,
                'prompt': prompt,
                'create_agent': True,
                'casual_conv': False,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            result = resp.json()
            return result.get('response', '')
    except requests.RequestException as e:
        logger.debug(f"Chat dispatch failed for goal {goal_id}: {e}")

    return None
