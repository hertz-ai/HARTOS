"""
HART OS Platform Layer — Durable, extensible OS substrate.

Provides the foundational abstractions that unify all HART OS subsystems:
- ServiceRegistry: Typed, lazy-loaded service container (replaces ad-hoc singletons)
- PlatformConfig: 3-layer config resolution (env > DB > defaults)
- EventBus: Topic-based pub/sub for decoupled communication
- AppManifest: Universal manifest schema for all app types
- AppRegistry: Discovery + lifecycle for panels, desktop apps, agents, extensions
- ExtensionRegistry: Platform-wide plugin system with hot-reload

Namespace lives under core/ (alongside config_cache, circuit_breaker, event_loop)
to avoid collision with Python's stdlib `platform` module.

Imports are lazy to allow each module to be used independently.
"""


def __getattr__(name):
    """Lazy imports — modules loaded on first access, not at import time."""
    if name in ('ServiceRegistry', 'get_registry', 'reset_registry'):
        from core.platform.registry import ServiceRegistry, get_registry, reset_registry
        return {'ServiceRegistry': ServiceRegistry, 'get_registry': get_registry,
                'reset_registry': reset_registry}[name]
    if name == 'PlatformConfig':
        from core.platform.config import PlatformConfig
        return PlatformConfig
    if name == 'EventBus':
        from core.platform.events import EventBus
        return EventBus
    if name in ('AppManifest', 'AppType'):
        from core.platform.app_manifest import AppManifest, AppType
        return {'AppManifest': AppManifest, 'AppType': AppType}[name]
    if name == 'AppRegistry':
        from core.platform.app_registry import AppRegistry
        return AppRegistry
    raise AttributeError(f"module 'core.platform' has no attribute {name!r}")


__all__ = [
    'ServiceRegistry',
    'get_registry',
    'reset_registry',
    'PlatformConfig',
    'EventBus',
    'AppManifest',
    'AppType',
    'AppRegistry',
]
