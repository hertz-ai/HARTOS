"""
App Manifest — Universal schema for all HART OS application types.

Every app in HART OS — Nunba panel, system panel, desktop app (RustDesk),
agent, MCP server, channel plugin, extension — is described by a single
AppManifest dataclass. This is the unifying abstraction.

Generalizes:
- shell_manifest.py: PANEL_MANIFEST, SYSTEM_PANELS, DYNAMIC_PANELS
- service_manager.py: EngineInfo for native desktop apps
- mcp_integration.py: MCP server descriptions
- plugin_system.py: PluginMetadata

The `entry` dict is type-specific:
    nunba_panel:   {'route': '/social'}
    system_panel:  {'api': '/api/shell/audio'}
    desktop_app:   {'exec': 'rustdesk', 'bridge': 'rustdesk_bridge'}
    service:       {'http': 'http://localhost:8080'}
    agent:         {'prompt_id': '123', 'flow_id': '0'}
    mcp_server:    {'mcp': 'filesystem'}
    channel:       {'adapter': 'discord'}
    extension:     {'module': 'extensions.my_ext'}
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class AppType(Enum):
    """All application types in HART OS."""
    NUNBA_PANEL = 'nunba_panel'         # Nunba SPA iframe panel
    SYSTEM_PANEL = 'system_panel'       # Native Python-rendered panel
    DYNAMIC_PANEL = 'dynamic_panel'     # Context-opened panel
    DESKTOP_APP = 'desktop_app'         # External native binary (Rust/C++)
    SERVICE = 'service'                 # Background service (llama.cpp, etc.)
    AGENT = 'agent'                     # AI agent
    MCP_SERVER = 'mcp_server'           # Model Context Protocol server
    CHANNEL = 'channel'                 # Channel adapter (Discord, Telegram)
    EXTENSION = 'extension'             # Platform extension/plugin


@dataclass
class AppManifest:
    """Universal manifest for any HART OS application.

    Every app type uses the same schema. The `type` field determines
    how the shell renders it and how lifecycle management works.
    """
    id: str                                     # Unique: 'feed', 'rustdesk', 'mcp_filesystem'
    name: str                                   # Display name: 'Feed', 'RustDesk'
    version: str                                # Semver or 'auto' for detected binaries
    type: str                                   # AppType value string
    icon: str = 'extension'                     # Material icon name or file path
    entry: Dict[str, Any] = field(default_factory=dict)   # Type-specific launch info
    group: str = ''                             # Start menu group: 'Discover', 'System'
    default_size: Tuple[int, int] = (800, 600)  # Default window size
    permissions: List[str] = field(default_factory=list)   # ['audio', 'display', 'network']
    apis: List[str] = field(default_factory=list)          # API endpoints used
    config_schema: Dict[str, Any] = field(default_factory=dict)  # Settings schema
    dependencies: List[str] = field(default_factory=list)  # Other app IDs required
    platforms: List[str] = field(default_factory=lambda: ['all'])  # ['linux', 'windows']
    singleton: bool = True                      # Only one instance at a time?
    auto_start: bool = False                    # Start with OS?
    tags: List[str] = field(default_factory=list)          # Search tags
    description: str = ''                       # Short description

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict (for API responses, JSON storage)."""
        return {
            'id': self.id,
            'name': self.name,
            'version': self.version,
            'type': self.type,
            'icon': self.icon,
            'entry': self.entry,
            'group': self.group,
            'default_size': list(self.default_size),
            'permissions': self.permissions,
            'apis': self.apis,
            'config_schema': self.config_schema,
            'dependencies': self.dependencies,
            'platforms': self.platforms,
            'singleton': self.singleton,
            'auto_start': self.auto_start,
            'tags': self.tags,
            'description': self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AppManifest':
        """Deserialize from dict."""
        d = dict(data)
        if 'default_size' in d:
            d['default_size'] = tuple(d['default_size'])
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})

    @classmethod
    def from_panel_manifest(cls, panel_id: str,
                            panel: Dict[str, Any]) -> 'AppManifest':
        """Convert a shell_manifest.py PANEL_MANIFEST entry to AppManifest.

        Backward compatibility bridge for existing panel definitions.
        """
        return cls(
            id=panel_id,
            name=panel.get('title', panel_id),
            version='1.0.0',
            type=AppType.NUNBA_PANEL.value,
            icon=panel.get('icon', 'extension'),
            entry={'route': panel.get('route', '')},
            group=panel.get('group', ''),
            default_size=tuple(panel.get('default_size', [800, 600])),
            apis=panel.get('apis', []),
            tags=panel.get('tags', []),
        )

    @classmethod
    def from_system_panel(cls, panel_id: str,
                          panel: Dict[str, Any]) -> 'AppManifest':
        """Convert a shell_manifest.py SYSTEM_PANELS entry to AppManifest."""
        return cls(
            id=panel_id,
            name=panel.get('title', panel_id),
            version='1.0.0',
            type=AppType.SYSTEM_PANEL.value,
            icon=panel.get('icon', 'settings'),
            entry={'loader': panel.get('loader', '')},
            group=panel.get('group', 'System'),
            default_size=tuple(panel.get('default_size', [700, 500])),
            apis=panel.get('apis', []),
            tags=panel.get('tags', []),
        )

    def matches_search(self, query: str) -> bool:
        """Check if this manifest matches a search query (case-insensitive)."""
        q = query.lower()
        return (q in self.id.lower()
                or q in self.name.lower()
                or q in self.description.lower()
                or q in self.group.lower()
                or any(q in tag.lower() for tag in self.tags))
