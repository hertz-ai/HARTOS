"""
Plugin System for HevolveBot Integration.

Provides base Plugin class and PluginManager for loading, unloading,
and managing plugins with lifecycle hooks.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from enum import Enum
import importlib
import sys
from datetime import datetime

logger = logging.getLogger(__name__)


class PluginState(Enum):
    """Plugin lifecycle states."""
    UNLOADED = "unloaded"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class PluginMetadata:
    """Metadata for a plugin."""
    name: str
    version: str
    description: str
    author: str = ""
    dependencies: List[str] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)


class Plugin(ABC):
    """
    Base class for all plugins.

    Plugins must implement lifecycle hooks and can optionally
    implement message processing hooks.
    """

    def __init__(self):
        self._state = PluginState.UNLOADED
        self._config: Dict[str, Any] = {}
        self._loaded_at: Optional[datetime] = None
        self._error_message: Optional[str] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the plugin name."""
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        """Return the plugin version."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Return the plugin description."""
        pass

    @property
    def state(self) -> PluginState:
        """Return the current plugin state."""
        return self._state

    @property
    def config(self) -> Dict[str, Any]:
        """Return the plugin configuration."""
        return self._config

    @property
    def loaded_at(self) -> Optional[datetime]:
        """Return when the plugin was loaded."""
        return self._loaded_at

    @property
    def error_message(self) -> Optional[str]:
        """Return any error message if plugin is in error state."""
        return self._error_message

    def get_metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name=self.name,
            version=self.version,
            description=self.description
        )

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the plugin with given settings."""
        self._config = config

    def on_load(self) -> bool:
        """
        Called when the plugin is loaded.

        Returns:
            True if load was successful, False otherwise.
        """
        return True

    def on_unload(self) -> bool:
        """
        Called when the plugin is unloaded.

        Returns:
            True if unload was successful, False otherwise.
        """
        return True

    def on_enable(self) -> bool:
        """
        Called when the plugin is enabled.

        Returns:
            True if enable was successful, False otherwise.
        """
        return True

    def on_disable(self) -> bool:
        """
        Called when the plugin is disabled.

        Returns:
            True if disable was successful, False otherwise.
        """
        return True

    def on_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Called when a message is received.

        Args:
            message: The incoming message.

        Returns:
            Modified message or None to pass through unchanged.
        """
        return None

    def on_response(self, response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Called when a response is about to be sent.

        Args:
            response: The outgoing response.

        Returns:
            Modified response or None to pass through unchanged.
        """
        return None

    def on_error(self, error: Exception) -> None:
        """
        Called when an error occurs during message processing.

        Args:
            error: The exception that occurred.
        """
        pass


class PluginManager:
    """
    Manages plugin lifecycle and message routing.

    Handles loading, unloading, enabling, and disabling plugins,
    as well as routing messages through active plugins.
    """

    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}
        self._load_order: List[str] = []
        self._message_hooks: List[Callable] = []
        self._response_hooks: List[Callable] = []

    @property
    def plugins(self) -> Dict[str, Plugin]:
        """Return all loaded plugins."""
        return self._plugins.copy()

    def load(self, plugin: Plugin, config: Optional[Dict[str, Any]] = None) -> bool:
        """
        Load a plugin.

        Args:
            plugin: The plugin instance to load.
            config: Optional configuration for the plugin.

        Returns:
            True if load was successful, False otherwise.
        """
        if plugin.name in self._plugins:
            logger.warning(f"Plugin {plugin.name} is already loaded")
            return False

        try:
            if config:
                plugin.configure(config)

            if plugin.on_load():
                plugin._state = PluginState.LOADED
                plugin._loaded_at = datetime.utcnow()
                self._plugins[plugin.name] = plugin
                self._load_order.append(plugin.name)
                logger.info(f"Plugin {plugin.name} v{plugin.version} loaded successfully")
                return True
            else:
                plugin._state = PluginState.ERROR
                plugin._error_message = "on_load() returned False"
                logger.error(f"Plugin {plugin.name} failed to load")
                return False
        except Exception as e:
            plugin._state = PluginState.ERROR
            plugin._error_message = str(e)
            logger.exception(f"Error loading plugin {plugin.name}: {e}")
            return False

    def load_from_module(self, module_path: str, class_name: str,
                         config: Optional[Dict[str, Any]] = None) -> bool:
        """
        Load a plugin from a module path.

        Args:
            module_path: The module path (e.g., 'my.plugins.example').
            class_name: The plugin class name.
            config: Optional configuration for the plugin.

        Returns:
            True if load was successful, False otherwise.
        """
        try:
            module = importlib.import_module(module_path)
            plugin_class = getattr(module, class_name)
            plugin = plugin_class()
            return self.load(plugin, config)
        except Exception as e:
            logger.exception(f"Error loading plugin from {module_path}.{class_name}: {e}")
            return False

    def unload(self, plugin_name: str) -> bool:
        """
        Unload a plugin.

        Args:
            plugin_name: The name of the plugin to unload.

        Returns:
            True if unload was successful, False otherwise.
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin {plugin_name} is not loaded")
            return False

        plugin = self._plugins[plugin_name]

        try:
            # Disable first if enabled
            if plugin.state == PluginState.ENABLED:
                self.disable(plugin_name)

            if plugin.on_unload():
                plugin._state = PluginState.UNLOADED
                del self._plugins[plugin_name]
                self._load_order.remove(plugin_name)
                logger.info(f"Plugin {plugin_name} unloaded successfully")
                return True
            else:
                logger.error(f"Plugin {plugin_name} failed to unload")
                return False
        except Exception as e:
            logger.exception(f"Error unloading plugin {plugin_name}: {e}")
            return False

    def enable(self, plugin_name: str) -> bool:
        """
        Enable a loaded plugin.

        Args:
            plugin_name: The name of the plugin to enable.

        Returns:
            True if enable was successful, False otherwise.
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin {plugin_name} is not loaded")
            return False

        plugin = self._plugins[plugin_name]

        if plugin.state == PluginState.ENABLED:
            logger.warning(f"Plugin {plugin_name} is already enabled")
            return True

        if plugin.state not in (PluginState.LOADED, PluginState.DISABLED):
            logger.error(f"Plugin {plugin_name} is in invalid state: {plugin.state}")
            return False

        try:
            if plugin.on_enable():
                plugin._state = PluginState.ENABLED
                # Register hooks
                if hasattr(plugin, 'on_message'):
                    self._message_hooks.append(plugin.on_message)
                if hasattr(plugin, 'on_response'):
                    self._response_hooks.append(plugin.on_response)
                logger.info(f"Plugin {plugin_name} enabled")
                return True
            else:
                logger.error(f"Plugin {plugin_name} failed to enable")
                return False
        except Exception as e:
            logger.exception(f"Error enabling plugin {plugin_name}: {e}")
            return False

    def disable(self, plugin_name: str) -> bool:
        """
        Disable an enabled plugin.

        Args:
            plugin_name: The name of the plugin to disable.

        Returns:
            True if disable was successful, False otherwise.
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin {plugin_name} is not loaded")
            return False

        plugin = self._plugins[plugin_name]

        if plugin.state != PluginState.ENABLED:
            logger.warning(f"Plugin {plugin_name} is not enabled")
            return True

        try:
            if plugin.on_disable():
                plugin._state = PluginState.DISABLED
                # Unregister hooks
                if plugin.on_message in self._message_hooks:
                    self._message_hooks.remove(plugin.on_message)
                if plugin.on_response in self._response_hooks:
                    self._response_hooks.remove(plugin.on_response)
                logger.info(f"Plugin {plugin_name} disabled")
                return True
            else:
                logger.error(f"Plugin {plugin_name} failed to disable")
                return False
        except Exception as e:
            logger.exception(f"Error disabling plugin {plugin_name}: {e}")
            return False

    def list_plugins(self) -> List[Dict[str, Any]]:
        """
        List all loaded plugins with their status.

        Returns:
            List of plugin information dictionaries.
        """
        result = []
        for name in self._load_order:
            plugin = self._plugins[name]
            result.append({
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "state": plugin.state.value,
                "loaded_at": plugin.loaded_at.isoformat() if plugin.loaded_at else None,
                "error": plugin.error_message
            })
        return result

    def get_plugin(self, plugin_name: str) -> Optional[Plugin]:
        """
        Get a plugin by name.

        Args:
            plugin_name: The name of the plugin.

        Returns:
            The plugin instance or None if not found.
        """
        return self._plugins.get(plugin_name)

    def process_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a message through all enabled plugins.

        Args:
            message: The incoming message.

        Returns:
            The processed message.
        """
        current_message = message.copy()

        for hook in self._message_hooks:
            try:
                result = hook(current_message)
                if result is not None:
                    current_message = result
            except Exception as e:
                logger.exception(f"Error in message hook: {e}")

        return current_message

    def process_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a response through all enabled plugins.

        Args:
            response: The outgoing response.

        Returns:
            The processed response.
        """
        current_response = response.copy()

        for hook in self._response_hooks:
            try:
                result = hook(current_response)
                if result is not None:
                    current_response = result
            except Exception as e:
                logger.exception(f"Error in response hook: {e}")

        return current_response

    def reload(self, plugin_name: str) -> bool:
        """
        Reload a plugin.

        Args:
            plugin_name: The name of the plugin to reload.

        Returns:
            True if reload was successful, False otherwise.
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin {plugin_name} is not loaded")
            return False

        plugin = self._plugins[plugin_name]
        was_enabled = plugin.state == PluginState.ENABLED
        config = plugin.config.copy()

        # Store plugin class for reinstantiation
        plugin_class = plugin.__class__

        if not self.unload(plugin_name):
            return False

        # Create new instance
        new_plugin = plugin_class()

        if not self.load(new_plugin, config):
            return False

        if was_enabled:
            return self.enable(plugin_name)

        return True

    def unload_all(self) -> bool:
        """
        Unload all plugins.

        Returns:
            True if all plugins were unloaded successfully.
        """
        success = True
        # Unload in reverse order
        for name in reversed(self._load_order.copy()):
            if not self.unload(name):
                success = False
        return success
