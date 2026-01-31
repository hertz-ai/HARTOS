"""
Plugin System for HevolveBot Integration.

Provides plugin management, HTTP server for plugin routes,
and a plugin registry for discovery and installation.
"""

from .plugin_system import (
    Plugin,
    PluginManager,
    PluginState,
    PluginMetadata
)
from .http_server import (
    PluginHTTPServer,
    HTTPMethod,
    Route,
    Request,
    Response
)
from .registry import (
    PluginRegistry,
    PluginInfo,
    PluginSource,
    InstalledPlugin
)

__all__ = [
    # Plugin system
    'Plugin',
    'PluginManager',
    'PluginState',
    'PluginMetadata',
    # HTTP server
    'PluginHTTPServer',
    'HTTPMethod',
    'Route',
    'Request',
    'Response',
    # Registry
    'PluginRegistry',
    'PluginInfo',
    'PluginSource',
    'InstalledPlugin',
]
