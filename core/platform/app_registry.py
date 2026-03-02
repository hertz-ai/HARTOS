"""
App Registry — Discovery and lifecycle for all HART OS applications.

Central catalog of every app in the OS — panels, desktop apps, agents,
MCP servers, extensions. Provides search, filtering, and backward
compatibility with shell_manifest.py.

Integrates with EventBus to emit app.registered / app.unregistered events.

Usage:
    registry = AppRegistry()
    registry.register(AppManifest(id='feed', name='Feed', ...))
    registry.list_by_group('Discover')   # -> [AppManifest, ...]
    registry.search('remote')            # -> [rustdesk, moonlight, ...]
    manifest = registry.to_shell_manifest()  # backward compat
"""

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from core.platform.app_manifest import AppManifest, AppType

logger = logging.getLogger('hevolve.platform')


class AppRegistry:
    """Central app catalog for HART OS.

    Stores AppManifest entries and provides query/search capabilities.
    Emits events via optional event_emitter callback.
    """

    def __init__(self, event_emitter: Optional[Callable] = None):
        """Initialize the registry.

        Args:
            event_emitter: Optional callable(topic, data) for event bus
                           integration. Called on register/unregister.
        """
        self._apps: Dict[str, AppManifest] = {}
        self._lock = threading.Lock()
        self._emit = event_emitter

    def register(self, manifest: AppManifest) -> None:
        """Register an app manifest.

        Args:
            manifest: The app manifest to register.

        Raises:
            ValueError: If app ID already registered.
        """
        with self._lock:
            if manifest.id in self._apps:
                raise ValueError(f"App '{manifest.id}' already registered")
            self._apps[manifest.id] = manifest

        if self._emit:
            self._emit('app.registered', {'app_id': manifest.id,
                                           'name': manifest.name,
                                           'type': manifest.type})

    def unregister(self, app_id: str) -> None:
        """Remove an app from the registry.

        Args:
            app_id: App ID to remove.

        Raises:
            KeyError: If app ID not found.
        """
        with self._lock:
            if app_id not in self._apps:
                raise KeyError(f"App '{app_id}' not registered")
            manifest = self._apps.pop(app_id)

        if self._emit:
            self._emit('app.unregistered', {'app_id': app_id,
                                             'name': manifest.name})

    def get(self, app_id: str) -> Optional[AppManifest]:
        """Get a specific app manifest by ID."""
        return self._apps.get(app_id)

    def list_all(self) -> List[AppManifest]:
        """Return all registered app manifests."""
        return list(self._apps.values())

    def list_by_type(self, app_type: str) -> List[AppManifest]:
        """Return apps matching a specific type.

        Args:
            app_type: AppType value string (e.g., 'nunba_panel', 'desktop_app').
        """
        return [m for m in self._apps.values() if m.type == app_type]

    def list_by_group(self, group: str) -> List[AppManifest]:
        """Return apps in a specific start menu group.

        Args:
            group: Group name (e.g., 'Discover', 'System', 'Remote').
        """
        return [m for m in self._apps.values()
                if m.group.lower() == group.lower()]

    def search(self, query: str) -> List[AppManifest]:
        """Fuzzy search across app name, ID, description, tags.

        Args:
            query: Search string (case-insensitive).

        Returns:
            Matching manifests, sorted by relevance (exact ID match first).
        """
        if not query:
            return self.list_all()

        results = [m for m in self._apps.values()
                   if m.matches_search(query)]

        # Sort: exact ID match first, then by name
        q_lower = query.lower()
        results.sort(key=lambda m: (
            0 if m.id.lower() == q_lower else
            1 if q_lower in m.id.lower() else
            2 if q_lower in m.name.lower() else 3,
            m.name.lower(),
        ))
        return results

    def groups(self) -> List[str]:
        """Return all unique group names."""
        seen = set()
        result = []
        for m in self._apps.values():
            if m.group and m.group not in seen:
                seen.add(m.group)
                result.append(m.group)
        return sorted(result)

    def count(self) -> int:
        """Return total number of registered apps."""
        return len(self._apps)

    # ── Backward Compatibility ────────────────────────────────

    def to_shell_manifest(self) -> Dict[str, Dict[str, Any]]:
        """Convert to shell_manifest.py PANEL_MANIFEST format.

        For backward compatibility with LiquidUI rendering.
        """
        result = {}
        for m in self._apps.values():
            if m.type in (AppType.NUNBA_PANEL.value,
                          AppType.SYSTEM_PANEL.value,
                          AppType.DYNAMIC_PANEL.value):
                result[m.id] = {
                    'title': m.name,
                    'icon': m.icon,
                    'route': m.entry.get('route', ''),
                    'group': m.group,
                    'default_size': list(m.default_size),
                    'apis': m.apis,
                }
        return result

    def load_panel_manifest(self, panels: Dict[str, dict]) -> int:
        """Bulk-import from shell_manifest.py PANEL_MANIFEST dict.

        Args:
            panels: Dict of panel_id -> panel dict from shell_manifest.py.

        Returns:
            Number of panels imported.
        """
        count = 0
        for panel_id, panel in panels.items():
            if panel_id not in self._apps:
                manifest = AppManifest.from_panel_manifest(panel_id, panel)
                with self._lock:
                    self._apps[manifest.id] = manifest
                count += 1
        return count

    def load_system_panels(self, panels: Dict[str, dict]) -> int:
        """Bulk-import from shell_manifest.py SYSTEM_PANELS dict.

        Args:
            panels: Dict of panel_id -> panel dict.

        Returns:
            Number of panels imported.
        """
        count = 0
        for panel_id, panel in panels.items():
            if panel_id not in self._apps:
                manifest = AppManifest.from_system_panel(panel_id, panel)
                with self._lock:
                    self._apps[manifest.id] = manifest
                count += 1
        return count

    # ── Lifecycle (for ServiceRegistry) ───────────────────────

    def health(self) -> dict:
        """Health report."""
        type_counts = {}
        for m in self._apps.values():
            type_counts[m.type] = type_counts.get(m.type, 0) + 1
        return {
            'status': 'ok',
            'total_apps': len(self._apps),
            'types': type_counts,
            'groups': self.groups(),
        }
