"""
Environment Client — Thin wrapper for EnvironmentManager.

Usage:
    from hart_sdk import environments

    env = environments.create('research', model_policy='local_only',
                               allowed_tools=['web_search', 'read_file'])
    env.check_tool('web_search')  # True
    result = env.infer('Summarize this paper')
    environments.destroy(env.env_id)
"""

from typing import Any, Dict, List, Optional


class EnvironmentClient:
    """Singleton environment client for HART OS agent environments."""

    def _get_manager(self):
        """Get EnvironmentManager from ServiceRegistry."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if registry.has('environments'):
                return registry.get('environments')
        except ImportError:
            pass
        return None

    def create(self, name: str, **kwargs) -> Any:
        """Create a new agent environment.

        Returns AgentEnvironment or None if unavailable.
        """
        mgr = self._get_manager()
        if mgr is None:
            return None
        return mgr.create(name, **kwargs)

    def get(self, env_id: str) -> Any:
        """Get an environment by ID.

        Returns AgentEnvironment or None.
        """
        mgr = self._get_manager()
        if mgr is None:
            return None
        return mgr.get(env_id)

    def destroy(self, env_id: str) -> bool:
        """Destroy an environment.

        Returns True if destroyed, False if not found or unavailable.
        """
        mgr = self._get_manager()
        if mgr is None:
            return False
        return mgr.destroy(env_id)

    def list_all(self) -> List[Dict[str, Any]]:
        """List all environments.

        Returns list of environment dicts or empty list.
        """
        mgr = self._get_manager()
        if mgr is None:
            return []
        return mgr.list_environments()


# Singleton
environments = EnvironmentClient()
