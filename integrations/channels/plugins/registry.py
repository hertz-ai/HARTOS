"""
Plugin Registry for HevolveBot Integration.

Provides a registry for discovering, installing, and managing plugins
from various sources.
"""

import logging
import json
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum
import os

logger = logging.getLogger(__name__)


class PluginSource(Enum):
    """Source types for plugins."""
    LOCAL = "local"
    REMOTE = "remote"
    GIT = "git"


@dataclass
class PluginInfo:
    """Information about an available plugin."""
    name: str
    version: str
    description: str
    author: str
    source: PluginSource
    source_url: str = ""
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    downloads: int = 0
    rating: float = 0.0
    checksum: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "source": self.source.value,
            "source_url": self.source_url,
            "dependencies": self.dependencies,
            "tags": self.tags,
            "downloads": self.downloads,
            "rating": self.rating,
            "checksum": self.checksum,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PluginInfo":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            version=data["version"],
            description=data.get("description", ""),
            author=data.get("author", ""),
            source=PluginSource(data.get("source", "local")),
            source_url=data.get("source_url", ""),
            dependencies=data.get("dependencies", []),
            tags=data.get("tags", []),
            downloads=data.get("downloads", 0),
            rating=data.get("rating", 0.0),
            checksum=data.get("checksum", ""),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
        )


@dataclass
class InstalledPlugin:
    """Information about an installed plugin."""
    info: PluginInfo
    install_path: str
    installed_at: datetime
    enabled: bool = False
    config: Dict[str, Any] = field(default_factory=dict)


class PluginRegistry:
    """
    Registry for plugin discovery and management.

    Handles searching for plugins, installing, uninstalling,
    and checking for updates.
    """

    def __init__(self, install_dir: str = "./plugins",
                 registry_url: str = ""):
        self._install_dir = install_dir
        self._registry_url = registry_url
        self._available_plugins: Dict[str, PluginInfo] = {}
        self._installed_plugins: Dict[str, InstalledPlugin] = {}
        self._cache_file = os.path.join(install_dir, ".registry_cache.json")

        # Ensure install directory exists
        os.makedirs(install_dir, exist_ok=True)

    @property
    def install_dir(self) -> str:
        """Return the plugin installation directory."""
        return self._install_dir

    @property
    def available_plugins(self) -> Dict[str, PluginInfo]:
        """Return available plugins."""
        return self._available_plugins.copy()

    @property
    def installed_plugins(self) -> Dict[str, InstalledPlugin]:
        """Return installed plugins."""
        return self._installed_plugins.copy()

    def register_plugin(self, info: PluginInfo) -> bool:
        """
        Register a plugin in the registry.

        Args:
            info: The plugin information.

        Returns:
            True if registration was successful.
        """
        if info.name in self._available_plugins:
            existing = self._available_plugins[info.name]
            # Update if newer version
            if self._compare_versions(info.version, existing.version) > 0:
                self._available_plugins[info.name] = info
                logger.info(f"Updated plugin {info.name} to v{info.version}")
            else:
                logger.debug(f"Plugin {info.name} already registered with same or newer version")
            return True

        self._available_plugins[info.name] = info
        logger.info(f"Registered plugin {info.name} v{info.version}")
        return True

    def unregister_plugin(self, name: str) -> bool:
        """
        Unregister a plugin from the registry.

        Args:
            name: The plugin name.

        Returns:
            True if unregistration was successful.
        """
        if name not in self._available_plugins:
            logger.warning(f"Plugin {name} not found in registry")
            return False

        del self._available_plugins[name]
        logger.info(f"Unregistered plugin {name}")
        return True

    def search(self, query: str = "", tags: Optional[List[str]] = None,
               author: str = "", min_rating: float = 0.0) -> List[PluginInfo]:
        """
        Search for plugins in the registry.

        Args:
            query: Search query string (searches name and description).
            tags: Filter by tags.
            author: Filter by author.
            min_rating: Minimum rating filter.

        Returns:
            List of matching plugins.
        """
        results = []
        query_lower = query.lower()

        for info in self._available_plugins.values():
            # Check query match
            if query and query_lower not in info.name.lower() and \
               query_lower not in info.description.lower():
                continue

            # Check tags
            if tags and not any(t in info.tags for t in tags):
                continue

            # Check author
            if author and author.lower() != info.author.lower():
                continue

            # Check rating
            if info.rating < min_rating:
                continue

            results.append(info)

        # Sort by downloads (popularity)
        results.sort(key=lambda x: x.downloads, reverse=True)
        return results

    def install(self, plugin_name: str, version: str = "",
                config: Optional[Dict[str, Any]] = None) -> Optional[InstalledPlugin]:
        """
        Install a plugin from the registry.

        Args:
            plugin_name: The name of the plugin to install.
            version: Specific version to install (latest if empty).
            config: Optional configuration for the plugin.

        Returns:
            InstalledPlugin if successful, None otherwise.
        """
        if plugin_name not in self._available_plugins:
            logger.error(f"Plugin {plugin_name} not found in registry")
            return None

        info = self._available_plugins[plugin_name]

        # Check version
        if version and version != info.version:
            logger.error(f"Version {version} not available for {plugin_name}")
            return None

        # Check if already installed
        if plugin_name in self._installed_plugins:
            installed = self._installed_plugins[plugin_name]
            if installed.info.version == info.version:
                logger.warning(f"Plugin {plugin_name} v{info.version} already installed")
                return installed
            logger.info(f"Upgrading {plugin_name} from v{installed.info.version} to v{info.version}")

        # Check dependencies
        for dep in info.dependencies:
            if dep not in self._installed_plugins:
                logger.warning(f"Missing dependency: {dep}")
                # Attempt to install dependency
                if not self.install(dep):
                    logger.error(f"Failed to install dependency {dep}")
                    return None

        # Create install path
        install_path = os.path.join(self._install_dir, plugin_name)
        os.makedirs(install_path, exist_ok=True)

        # Create installed plugin record
        installed = InstalledPlugin(
            info=info,
            install_path=install_path,
            installed_at=datetime.utcnow(),
            config=config or {}
        )

        self._installed_plugins[plugin_name] = installed
        logger.info(f"Installed plugin {plugin_name} v{info.version}")

        # Save cache
        self._save_cache()

        return installed

    def uninstall(self, plugin_name: str, remove_config: bool = False) -> bool:
        """
        Uninstall a plugin.

        Args:
            plugin_name: The name of the plugin to uninstall.
            remove_config: Whether to remove configuration files.

        Returns:
            True if uninstallation was successful.
        """
        if plugin_name not in self._installed_plugins:
            logger.warning(f"Plugin {plugin_name} is not installed")
            return False

        installed = self._installed_plugins[plugin_name]

        # Check for dependents
        dependents = self._find_dependents(plugin_name)
        if dependents:
            logger.error(f"Cannot uninstall {plugin_name}: required by {dependents}")
            return False

        # Remove installation
        if remove_config and os.path.exists(installed.install_path):
            try:
                import shutil
                shutil.rmtree(installed.install_path)
            except Exception as e:
                logger.warning(f"Could not remove install directory: {e}")

        del self._installed_plugins[plugin_name]
        logger.info(f"Uninstalled plugin {plugin_name}")

        # Save cache
        self._save_cache()

        return True

    def update(self, plugin_name: str) -> Optional[InstalledPlugin]:
        """
        Update a plugin to the latest version.

        Args:
            plugin_name: The name of the plugin to update.

        Returns:
            Updated InstalledPlugin if successful, None otherwise.
        """
        if plugin_name not in self._installed_plugins:
            logger.warning(f"Plugin {plugin_name} is not installed")
            return None

        installed = self._installed_plugins[plugin_name]

        if plugin_name not in self._available_plugins:
            logger.warning(f"Plugin {plugin_name} not found in registry")
            return None

        available = self._available_plugins[plugin_name]

        # Check if update is needed
        if self._compare_versions(available.version, installed.info.version) <= 0:
            logger.info(f"Plugin {plugin_name} is already up to date")
            return installed

        # Preserve config
        config = installed.config.copy()

        # Reinstall with new version
        return self.install(plugin_name, config=config)

    def check_updates(self) -> List[Dict[str, Any]]:
        """
        Check for available updates for installed plugins.

        Returns:
            List of plugins with available updates.
        """
        updates = []

        for name, installed in self._installed_plugins.items():
            if name in self._available_plugins:
                available = self._available_plugins[name]
                if self._compare_versions(available.version, installed.info.version) > 0:
                    updates.append({
                        "name": name,
                        "current_version": installed.info.version,
                        "available_version": available.version,
                        "description": available.description
                    })

        return updates

    def refresh(self) -> bool:
        """
        Refresh the registry from remote sources.

        Returns:
            True if refresh was successful.
        """
        # In a real implementation, this would fetch from remote registry
        logger.info("Refreshing plugin registry")
        return True

    def _find_dependents(self, plugin_name: str) -> List[str]:
        """Find plugins that depend on the given plugin."""
        dependents = []
        for name, installed in self._installed_plugins.items():
            if plugin_name in installed.info.dependencies:
                dependents.append(name)
        return dependents

    def _compare_versions(self, v1: str, v2: str) -> int:
        """
        Compare two version strings.

        Returns:
            1 if v1 > v2, -1 if v1 < v2, 0 if equal.
        """
        def parse_version(v):
            return [int(x) for x in v.split('.')]

        try:
            parts1 = parse_version(v1)
            parts2 = parse_version(v2)

            for p1, p2 in zip(parts1, parts2):
                if p1 > p2:
                    return 1
                if p1 < p2:
                    return -1

            if len(parts1) > len(parts2):
                return 1
            if len(parts1) < len(parts2):
                return -1

            return 0
        except:
            # Fall back to string comparison
            if v1 > v2:
                return 1
            if v1 < v2:
                return -1
            return 0

    def _save_cache(self) -> None:
        """Save the registry cache to disk."""
        try:
            cache_data = {
                "available": {k: v.to_dict() for k, v in self._available_plugins.items()},
                "installed": {
                    k: {
                        "info": v.info.to_dict(),
                        "install_path": v.install_path,
                        "installed_at": v.installed_at.isoformat(),
                        "enabled": v.enabled,
                        "config": v.config
                    }
                    for k, v in self._installed_plugins.items()
                }
            }
            with open(self._cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save registry cache: {e}")

    def _load_cache(self) -> None:
        """Load the registry cache from disk."""
        if not os.path.exists(self._cache_file):
            return

        try:
            with open(self._cache_file, 'r') as f:
                cache_data = json.load(f)

            for name, data in cache_data.get("available", {}).items():
                self._available_plugins[name] = PluginInfo.from_dict(data)

            for name, data in cache_data.get("installed", {}).items():
                self._installed_plugins[name] = InstalledPlugin(
                    info=PluginInfo.from_dict(data["info"]),
                    install_path=data["install_path"],
                    installed_at=datetime.fromisoformat(data["installed_at"]),
                    enabled=data.get("enabled", False),
                    config=data.get("config", {})
                )
        except Exception as e:
            logger.warning(f"Failed to load registry cache: {e}")

    def get_plugin_info(self, plugin_name: str) -> Optional[PluginInfo]:
        """Get information about a plugin."""
        return self._available_plugins.get(plugin_name)

    def is_installed(self, plugin_name: str) -> bool:
        """Check if a plugin is installed."""
        return plugin_name in self._installed_plugins

    def get_installed_version(self, plugin_name: str) -> Optional[str]:
        """Get the installed version of a plugin."""
        if plugin_name in self._installed_plugins:
            return self._installed_plugins[plugin_name].info.version
        return None
