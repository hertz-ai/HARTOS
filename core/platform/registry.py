"""
Service Registry — Typed, lazy-loaded, thread-safe service container.

Replaces the ad-hoc `_instance = None` + `get_*()` singleton pattern used
in 6+ modules across the codebase with a unified registry.

Design decisions:
- String-named services (not type-keyed) — allows swapping implementations
- Lazy instantiation — factory not called until first get()
- Singleton by default — one instance per name; factory mode available
- Optional Lifecycle protocol — start()/stop()/health() for managed services
- Dependency ordering — ensures services start in correct order
- Thread-safe — all mutations under threading.Lock

Generalizes patterns from:
- circuit_breaker.py (threading.Lock, state machine)
- service_manager.py (EngineService lifecycle, ServiceManager coordinator)
- Module-level singletons across agent_engine/

Usage:
    registry = get_registry()
    registry.register('theme', ThemeService)
    registry.register('events', EventBus, singleton=True)
    registry.register('compute', ComputeService, depends_on=['events'])

    theme = registry.get('theme')       # instantiates on first call
    registry.start_all()                # calls .start() in dependency order
    registry.health()                   # -> {name: {status, uptime, error}}
    registry.stop_all()                 # calls .stop() in reverse order
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger('hevolve.platform')


# ═══════════════════════════════════════════════════════════════
# Lifecycle Protocol
# ═══════════════════════════════════════════════════════════════

class Lifecycle:
    """Optional protocol for services that need managed start/stop.

    Services that implement start()/stop()/health() get lifecycle
    management from the registry. Plain objects work fine too —
    they just don't participate in start_all()/stop_all().
    """

    def start(self) -> None:
        """Called during registry.start_all() in dependency order."""
        pass

    def stop(self) -> None:
        """Called during registry.stop_all() in reverse dependency order."""
        pass

    def health(self) -> dict:
        """Return health status. Default: always healthy."""
        return {'status': 'ok'}


# ═══════════════════════════════════════════════════════════════
# Service Entry (internal)
# ═══════════════════════════════════════════════════════════════

class _ServiceEntry:
    """Internal record for a registered service."""

    __slots__ = ('name', 'factory', 'singleton', 'depends_on',
                 'instance', 'started', 'started_at', 'error')

    def __init__(self, name: str, factory: Callable, singleton: bool,
                 depends_on: List[str]):
        self.name = name
        self.factory = factory
        self.singleton = singleton
        self.depends_on = list(depends_on)
        self.instance: Any = None
        self.started: bool = False
        self.started_at: Optional[float] = None
        self.error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# Service Registry
# ═══════════════════════════════════════════════════════════════

class ServiceRegistry:
    """Typed, lazy-loaded, thread-safe service container.

    The central nervous system of HART OS — every subsystem registers
    its services here, and any subsystem can look up any other.
    """

    def __init__(self):
        self._entries: Dict[str, _ServiceEntry] = {}
        self._lock = threading.Lock()
        self._start_order: List[str] = []  # tracks actual start order for stop

    def register(self, name: str, factory: Callable, *,
                 singleton: bool = True,
                 depends_on: Optional[List[str]] = None) -> None:
        """Register a service factory.

        Args:
            name: Unique service name (e.g., 'theme', 'events', 'compute')
            factory: Any callable that returns the service instance.
                     Can be a class, function, or lambda.
            singleton: If True (default), factory called once and cached.
                       If False, factory called on every get().
            depends_on: List of service names that must be started before
                        this one. Only affects start_all() ordering.

        Raises:
            ValueError: If name is already registered.
        """
        with self._lock:
            if name in self._entries:
                raise ValueError(f"Service '{name}' already registered")
            self._entries[name] = _ServiceEntry(
                name=name,
                factory=factory,
                singleton=singleton,
                depends_on=depends_on or [],
            )

    def unregister(self, name: str) -> None:
        """Remove a service. Stops it first if running.

        Args:
            name: Service name to remove.

        Raises:
            KeyError: If name not registered.
        """
        with self._lock:
            if name not in self._entries:
                raise KeyError(f"Service '{name}' not registered")
            entry = self._entries[name]
            if entry.started and entry.instance and hasattr(entry.instance, 'stop'):
                try:
                    entry.instance.stop()
                except Exception as e:
                    logger.warning("Error stopping service '%s': %s", name, e)
            if name in self._start_order:
                self._start_order.remove(name)
            del self._entries[name]

    def get(self, name: str) -> Any:
        """Get a service instance. Instantiates lazily on first call.

        Args:
            name: Service name.

        Returns:
            The service instance.

        Raises:
            KeyError: If name not registered.
            RuntimeError: If factory raises during instantiation.
        """
        with self._lock:
            if name not in self._entries:
                raise KeyError(f"Service '{name}' not registered")
            entry = self._entries[name]

            # Non-singleton: always create new
            if not entry.singleton:
                try:
                    return entry.factory()
                except Exception as e:
                    logger.error("Factory failed for '%s': %s", name, e)
                    raise RuntimeError(
                        f"Failed to create service '{name}': {e}") from e

            # Singleton: return cached or create
            if entry.instance is not None:
                return entry.instance

            try:
                entry.instance = entry.factory()
                return entry.instance
            except Exception as e:
                entry.error = str(e)
                logger.error("Factory failed for '%s': %s", name, e)
                raise RuntimeError(
                    f"Failed to create service '{name}': {e}") from e

    def has(self, name: str) -> bool:
        """Check if a service is registered."""
        return name in self._entries

    def names(self) -> List[str]:
        """Return all registered service names."""
        return list(self._entries.keys())

    # ── Lifecycle Management ──────────────────────────────────

    def start_all(self) -> None:
        """Start all lifecycle-aware services in dependency order.

        Services without start() are skipped. Dependencies are resolved
        via topological sort — if A depends_on B, B starts first.
        """
        order = self._resolve_start_order()
        for name in order:
            self._start_service(name)

    def stop_all(self) -> None:
        """Stop all started services in reverse start order."""
        for name in reversed(list(self._start_order)):
            self._stop_service(name)

    def health(self) -> Dict[str, dict]:
        """Return health status for all registered services.

        Returns:
            Dict mapping service name to health dict.
            Non-lifecycle services report {'status': 'registered'}.
        """
        result = {}
        with self._lock:
            for name, entry in self._entries.items():
                if entry.error:
                    result[name] = {'status': 'error', 'error': entry.error}
                elif entry.instance is None:
                    result[name] = {'status': 'not_instantiated'}
                elif not entry.started:
                    result[name] = {'status': 'instantiated'}
                elif hasattr(entry.instance, 'health'):
                    try:
                        h = entry.instance.health()
                        if entry.started_at:
                            h['uptime_seconds'] = int(
                                time.time() - entry.started_at)
                        result[name] = h
                    except Exception as e:
                        result[name] = {'status': 'error', 'error': str(e)}
                else:
                    info = {'status': 'running'}
                    if entry.started_at:
                        info['uptime_seconds'] = int(
                            time.time() - entry.started_at)
                    result[name] = info
        return result

    def reset(self) -> None:
        """Stop all services and clear the registry. For testing."""
        self.stop_all()
        with self._lock:
            self._entries.clear()
            self._start_order.clear()

    # ── Internal ──────────────────────────────────────────────

    def _start_service(self, name: str) -> None:
        """Start a single service if it has a start() method."""
        with self._lock:
            entry = self._entries.get(name)
            if not entry or entry.started:
                return

            # Ensure instance exists
            if entry.instance is None and entry.singleton:
                try:
                    entry.instance = entry.factory()
                except Exception as e:
                    entry.error = str(e)
                    logger.error("Factory failed for '%s': %s", name, e)
                    return

            instance = entry.instance
            if instance and hasattr(instance, 'start'):
                try:
                    instance.start()
                    entry.started = True
                    entry.started_at = time.time()
                    entry.error = None
                    self._start_order.append(name)
                    logger.debug("Started service '%s'", name)
                except Exception as e:
                    entry.error = str(e)
                    logger.error("Failed to start '%s': %s", name, e)
            else:
                # No start() — mark as started anyway for tracking
                entry.started = True
                entry.started_at = time.time()
                self._start_order.append(name)

    def _stop_service(self, name: str) -> None:
        """Stop a single service if it has a stop() method."""
        with self._lock:
            entry = self._entries.get(name)
            if not entry or not entry.started:
                return

            if entry.instance and hasattr(entry.instance, 'stop'):
                try:
                    entry.instance.stop()
                    logger.debug("Stopped service '%s'", name)
                except Exception as e:
                    logger.warning("Error stopping '%s': %s", name, e)

            entry.started = False
            entry.started_at = None
            if name in self._start_order:
                self._start_order.remove(name)

    def _resolve_start_order(self) -> List[str]:
        """Topological sort of services by depends_on.

        Returns list of service names in safe start order.
        Raises ValueError on circular dependencies.
        """
        with self._lock:
            # Kahn's algorithm
            in_degree: Dict[str, int] = {}
            graph: Dict[str, List[str]] = {}

            for name, entry in self._entries.items():
                in_degree.setdefault(name, 0)
                graph.setdefault(name, [])
                for dep in entry.depends_on:
                    if dep in self._entries:
                        graph.setdefault(dep, []).append(name)
                        in_degree[name] = in_degree.get(name, 0) + 1

            queue = [n for n, d in in_degree.items() if d == 0]
            order: List[str] = []

            while queue:
                node = queue.pop(0)
                order.append(node)
                for neighbor in graph.get(node, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

            if len(order) != len(self._entries):
                missing = set(self._entries.keys()) - set(order)
                raise ValueError(
                    f"Circular dependency detected among: {missing}")

            return order


# ═══════════════════════════════════════════════════════════════
# Global Singleton
# ═══════════════════════════════════════════════════════════════

_registry: Optional[ServiceRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ServiceRegistry:
    """Get the global ServiceRegistry singleton.

    Thread-safe. Creates the registry on first call.
    """
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = ServiceRegistry()
        return _registry


def reset_registry() -> None:
    """Reset the global registry. For testing only."""
    global _registry
    with _registry_lock:
        if _registry:
            _registry.reset()
        _registry = None
