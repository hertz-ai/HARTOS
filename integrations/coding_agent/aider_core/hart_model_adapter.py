"""
HARTOS Model Adapter — Bridges HARTOS LLM infrastructure to Aider's Model interface.

Aider's repomap.py calls self.main_model.token_count(text) for sizing the repo map.
This adapter provides that method using tiktoken (already a HARTOS dependency),
without requiring LiteLLM.

For actual LLM completions (used by the HartCoder), this adapter routes through
HARTOS's existing OpenAI client and budget gate.
"""
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.coding_agent.aider_core')

# Lazy tiktoken import — available in HARTOS venv
_encoder = None


def _get_encoder():
    """Get or create a tiktoken encoder (cached)."""
    global _encoder
    if _encoder is None:
        try:
            import tiktoken
            _encoder = tiktoken.encoding_for_model('gpt-4')
        except ImportError:
            logger.warning("tiktoken not available, using approximate token counting")
            _encoder = _ApproximateEncoder()
        except Exception:
            _encoder = _ApproximateEncoder()
    return _encoder


class _ApproximateEncoder:
    """Fallback token counter when tiktoken isn't available (~4 chars per token)."""

    def encode(self, text):
        return [0] * (len(text) // 4)


class HartModelAdapter:
    """Minimal model adapter that satisfies Aider's Model interface for repomap.

    Only implements what repomap.py actually calls:
    - token_count(text) -> int
    """

    def __init__(self, model_name: str = 'gpt-4', max_context_window: int = 128000):
        self.name = model_name
        self.max_context_window = max_context_window

    def token_count(self, text: str) -> int:
        """Count tokens in text using tiktoken."""
        if not text:
            return 0
        encoder = _get_encoder()
        return len(encoder.encode(text))

    @classmethod
    def from_hartos_config(cls) -> 'HartModelAdapter':
        """Create adapter from HARTOS configuration.

        Uses model_registry if available, falls back to env/defaults.
        """
        model_name = os.environ.get('HEVOLVE_CODING_MODEL', 'gpt-4')
        max_ctx = 128000

        try:
            from integrations.agent_engine.model_registry import get_model_by_policy
            model_info = get_model_by_policy()
            if model_info:
                model_name = model_info.get('model_id', model_name)
                max_ctx = model_info.get('context_window', max_ctx)
        except ImportError:
            pass

        return cls(model_name=model_name, max_context_window=max_ctx)


def send_completion(
    messages: List[Dict],
    model: str = '',
    temperature: float = 0.0,
    max_tokens: int = 4096,
    user_id: str = '',
    prompt_id: str = '',
) -> Optional[str]:
    """Send a completion request through HARTOS's LLM infrastructure.

    Routes through budget gate for cost tracking. Used by HartCoder
    for code editing tasks.

    Returns:
        Response text or None on failure.
    """
    if not model:
        model = os.environ.get('HEVOLVE_CODING_MODEL', 'gpt-4')

    try:
        import openai
        from helper import get_openai_config

        config = get_openai_config()
        client_kwargs = {}
        if config.get('api_key'):
            client_kwargs['api_key'] = config['api_key']
        if config.get('api_base'):
            client_kwargs['api_base'] = config['api_base']

        response = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **client_kwargs,
        )

        result_text = response.choices[0].message.content

        # Record metered usage if budget gate available
        try:
            from integrations.agent_engine.budget_gate import record_metered_usage
            usage = response.get('usage', {})
            record_metered_usage(
                user_id=user_id or 'coding_agent',
                model=model,
                prompt_tokens=usage.get('prompt_tokens', 0),
                completion_tokens=usage.get('completion_tokens', 0),
                source='aider_native',
            )
        except ImportError:
            pass

        return result_text

    except Exception as e:
        logger.error(f"Completion failed: {e}")
        return None
