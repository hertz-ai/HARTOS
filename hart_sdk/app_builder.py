"""
HartApp — Fluent builder for HART OS app manifests.

Usage:
    app = HartApp('translator', version='1.0.0')
    app.needs_ai('llm', min_accuracy=0.7)
    app.needs_ai('tts', required=False)
    app.permissions(['network', 'audio'])
    app.group('Productivity')
    app.tags(['translate', 'language'])

    manifest = app.manifest()      # -> AppManifest
    app.register()                 # Register with OS AppRegistry
"""

from typing import Any, Dict, List, Optional, Tuple


class HartApp:
    """Fluent builder for HART OS applications."""

    def __init__(self, app_id: str, name: Optional[str] = None,
                 version: str = '1.0.0', app_type: str = 'extension'):
        self._id = app_id
        self._name = name or app_id.replace('-', ' ').replace('_', ' ').title()
        self._version = version
        self._type = app_type
        self._icon = 'extension'
        self._entry: Dict[str, Any] = {}
        self._group = ''
        self._default_size: Tuple[int, int] = (800, 600)
        self._permissions: List[str] = []
        self._apis: List[str] = []
        self._tags: List[str] = []
        self._description = ''
        self._ai_capabilities: List[Dict[str, Any]] = []
        self._dependencies: List[str] = []
        self._platforms: List[str] = ['all']
        self._singleton = True
        self._auto_start = False

    def needs_ai(self, capability_type: str, required: bool = True,
                 local_only: bool = False, min_accuracy: float = 0.0,
                 max_latency_ms: float = 0.0, max_cost_spark: float = 0.0,
                 **options) -> 'HartApp':
        """Declare an AI capability this app needs from the OS."""
        self._ai_capabilities.append({
            'type': capability_type,
            'required': required,
            'local_only': local_only,
            'min_accuracy': min_accuracy,
            'max_latency_ms': max_latency_ms,
            'max_cost_spark': max_cost_spark,
            'options': options,
        })
        return self

    def permissions(self, perms: List[str]) -> 'HartApp':
        """Set required permissions."""
        self._permissions = perms
        return self

    def group(self, group_name: str) -> 'HartApp':
        """Set start menu group."""
        self._group = group_name
        return self

    def tags(self, tag_list: List[str]) -> 'HartApp':
        """Set search tags."""
        self._tags = tag_list
        return self

    def icon(self, icon_name: str) -> 'HartApp':
        """Set Material icon name."""
        self._icon = icon_name
        return self

    def description(self, desc: str) -> 'HartApp':
        """Set app description."""
        self._description = desc
        return self

    def entry(self, **kwargs) -> 'HartApp':
        """Set type-specific launch configuration."""
        self._entry = kwargs
        return self

    def size(self, width: int, height: int) -> 'HartApp':
        """Set default window size."""
        self._default_size = (width, height)
        return self

    def depends_on(self, *app_ids: str) -> 'HartApp':
        """Declare dependency on other apps."""
        self._dependencies.extend(app_ids)
        return self

    def platforms(self, platform_list: List[str]) -> 'HartApp':
        """Set supported platforms."""
        self._platforms = platform_list
        return self

    def manifest(self):
        """Build and return a validated AppManifest.

        Raises ValueError if validation fails.
        """
        try:
            from core.platform.app_manifest import AppManifest
            from core.platform.manifest_validator import ManifestValidator
            m = AppManifest(
                id=self._id,
                name=self._name,
                version=self._version,
                type=self._type,
                icon=self._icon,
                entry=self._entry,
                group=self._group,
                default_size=self._default_size,
                permissions=self._permissions,
                apis=self._apis,
                tags=self._tags,
                description=self._description,
                ai_capabilities=self._ai_capabilities,
                dependencies=self._dependencies,
                platforms=self._platforms,
                singleton=self._singleton,
                auto_start=self._auto_start,
            )
            valid, errors = ManifestValidator.validate(m)
            if not valid:
                raise ValueError(
                    f"HartApp validation failed: {'; '.join(errors)}")
            return m
        except ImportError:
            return self._to_dict()

    def register(self) -> bool:
        """Register this app with the OS AppRegistry.

        Returns True if registered, False if platform unavailable.
        """
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('apps'):
                return False
            apps = registry.get('apps')
            apps.register(self.manifest())
            return True
        except Exception:
            return False

    def _to_dict(self) -> Dict[str, Any]:
        """Fallback dict representation when platform not available."""
        return {
            'id': self._id,
            'name': self._name,
            'version': self._version,
            'type': self._type,
            'icon': self._icon,
            'entry': self._entry,
            'group': self._group,
            'default_size': list(self._default_size),
            'permissions': self._permissions,
            'tags': self._tags,
            'description': self._description,
            'ai_capabilities': self._ai_capabilities,
            'dependencies': self._dependencies,
            'platforms': self._platforms,
        }
