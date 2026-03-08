"""
AI Client — Thin wrapper for AI inference and capability resolution.

Usage:
    from hart_sdk import ai

    result = ai.infer('Translate: Hello', model_type='llm')
    models = ai.list_models('tts')
    can_do = ai.can_satisfy([ai.capability('llm'), ai.capability('tts')])
"""

from typing import Any, Dict, List, Optional


class AIClient:
    """Singleton AI client for HART OS inference."""

    def infer(self, prompt: str, model_type: str = 'llm',
              options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch inference to ModelBusService.

        Args:
            prompt: The input prompt.
            model_type: 'llm', 'tts', 'stt', 'vision', etc.
            options: Additional options (policy, temperature, etc.)

        Returns:
            Result dict or error dict.
        """
        try:
            from integrations.agent_engine.model_bus_service import (
                get_model_bus_service,
            )
            bus = get_model_bus_service()
            if bus is None:
                return {'error': 'model bus service not available'}
            result = bus.infer(prompt=prompt, model_type=model_type,
                               options=options or {})
            return result if isinstance(result, dict) else {'result': result}
        except ImportError:
            return {'error': 'HART OS platform not available'}
        except Exception as e:
            return {'error': str(e)}

    def list_models(self, model_type: str = 'llm') -> List[Dict[str, Any]]:
        """List available models of a given type.

        Returns:
            List of model info dicts, or empty list if unavailable.
        """
        try:
            from integrations.agent_engine.model_registry import model_registry
            if model_registry is None:
                return []
            models = model_registry.list_models(model_type=model_type)
            return [m.to_dict() if hasattr(m, 'to_dict') else {'model_id': str(m)}
                    for m in models]
        except (ImportError, AttributeError):
            return []

    def capability(self, capability_type: str, **kwargs) -> Any:
        """Create an AICapability declaration.

        Args:
            capability_type: 'llm', 'tts', 'vision', etc.
            **kwargs: required, local_only, min_accuracy, etc.

        Returns:
            AICapability instance or dict fallback.
        """
        try:
            from core.platform.ai_capabilities import AICapability
            return AICapability(type=capability_type, **kwargs)
        except ImportError:
            return {'type': capability_type, **kwargs}

    def can_satisfy(self, capabilities: list) -> bool:
        """Check if the OS can satisfy all required capabilities.

        Args:
            capabilities: List of AICapability or dicts.

        Returns:
            True if all required capabilities satisfied, False otherwise.
        """
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('capability_router'):
                return False
            router = registry.get('capability_router')
            return router.can_satisfy(capabilities)
        except Exception:
            return False


# Singleton
ai = AIClient()
