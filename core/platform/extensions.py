"""
Extension System — Platform-wide plugin architecture.

Generalizes integrations/channels/plugins/plugin_system.py from
channels-only to platform-wide. Extensions can:
- Register services in the ServiceRegistry
- Subscribe to events on the EventBus
- Read/write PlatformConfig
- Provide an AppManifest (auto-registered in AppRegistry)
- Hot-reload without OS restart

State machine (from plugin_system.py):
    UNLOADED → LOADED → ENABLED ⇄ DISABLED → UNLOADED
                   ↓         ↓
                  ERROR     ERROR

Usage:
    class MyExtension(Extension):
        @property
        def manifest(self):
            return AppManifest(id='my_ext', name='My Extension', ...)

        def on_load(self, registry, config):
            self._svc = MyService()
            registry.register('my_service', lambda: self._svc)

        def on_enable(self):
            self._svc.start()

        def on_disable(self):
            self._svc.stop()
"""

import importlib
import importlib.util
import logging
import sys
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from core.platform.app_manifest import AppManifest

logger = logging.getLogger('hevolve.platform')


class ExtensionState(Enum):
    """Extension lifecycle states."""
    UNLOADED = 'unloaded'
    LOADED = 'loaded'
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    ERROR = 'error'


class Extension(ABC):
    """Base class for HART OS extensions.

    Subclass this to create a platform extension. Must provide
    a manifest and lifecycle hooks.
    """

    def __init__(self):
        self._state = ExtensionState.UNLOADED
        self._loaded_at: Optional[datetime] = None
        self._error: Optional[str] = None

    @property
    @abstractmethod
    def manifest(self) -> AppManifest:
        """Return the extension's AppManifest."""
        ...

    @property
    def state(self) -> ExtensionState:
        return self._state

    @property
    def error(self) -> Optional[str]:
        return self._error

    def on_load(self, registry: Any, config: Any) -> None:
        """Called when extension is loaded. Register services here.

        Args:
            registry: The ServiceRegistry — register services here.
            config: PlatformConfig access — read/write settings.
        """
        pass

    def on_enable(self) -> None:
        """Called when extension is enabled (activated)."""
        pass

    def on_disable(self) -> None:
        """Called when extension is disabled (deactivated)."""
        pass

    def on_unload(self) -> None:
        """Called when extension is unloaded (removed from memory)."""
        pass


class ExtensionRegistry:
    """Manages the lifecycle of platform extensions.

    Generalizes PluginManager from plugin_system.py to work at
    the platform level with ServiceRegistry and EventBus integration.
    """

    def __init__(self, service_registry: Any = None,
                 platform_config: Any = None,
                 event_emitter: Any = None):
        """Initialize the extension registry.

        Args:
            service_registry: ServiceRegistry for extensions to register services.
            platform_config: PlatformConfig for extensions to read/write settings.
            event_emitter: Optional callable(topic, data) for event bus integration.
        """
        self._extensions: Dict[str, Extension] = {}
        self._modules: Dict[str, str] = {}  # ext_id -> module_path
        self._registry = service_registry
        self._config = platform_config
        self._emit = event_emitter
        self._lock = threading.Lock()

    def load(self, module_path: str) -> Extension:
        """Load an extension from a Python module path.

        The module must contain a class that subclasses Extension.
        The first Extension subclass found is instantiated.

        Args:
            module_path: Dotted module path (e.g., 'extensions.my_ext').

        Returns:
            The loaded Extension instance.

        Raises:
            ImportError: If module not found.
            TypeError: If no Extension subclass found.
            ValueError: If extension ID already loaded.
        """
        # Sandbox analysis before import — block dangerous patterns
        from core.platform.extension_sandbox import ExtensionSandbox
        spec = importlib.util.find_spec(module_path)
        if spec and spec.origin and spec.origin.endswith('.py'):
            safe, violations = ExtensionSandbox.analyze_file(spec.origin)
            if not safe:
                # Emit security event + audit log (non-blocking)
                try:
                    from core.platform.events import emit_event
                    emit_event('security.extension_blocked', {
                        'module': module_path,
                        'violations': violations,
                    })
                except Exception:
                    pass
                try:
                    from security.immutable_audit_log import get_audit_log
                    get_audit_log().log_event(
                        'security', 'extension_sandbox',
                        f"Blocked extension '{module_path}'",
                        detail={'violations': violations})
                except Exception:
                    pass
                raise ImportError(
                    f"Extension '{module_path}' blocked by sandbox: "
                    f"{'; '.join(violations)}")

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"Cannot load extension module '{module_path}': {e}")

        # Find the Extension subclass
        ext_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and issubclass(attr, Extension)
                    and attr is not Extension):
                ext_class = attr
                break

        if ext_class is None:
            raise TypeError(
                f"No Extension subclass found in '{module_path}'")

        ext = ext_class()
        ext_id = ext.manifest.id

        with self._lock:
            if ext_id in self._extensions:
                raise ValueError(f"Extension '{ext_id}' already loaded")

        # Call on_load
        try:
            ext.on_load(self._registry, self._config)
            ext._state = ExtensionState.LOADED
            ext._loaded_at = datetime.now()
        except Exception as e:
            ext._state = ExtensionState.ERROR
            ext._error = str(e)
            logger.error("Extension '%s' on_load failed: %s", ext_id, e)
            raise

        with self._lock:
            self._extensions[ext_id] = ext
            self._modules[ext_id] = module_path

        if self._emit:
            self._emit('extension.loaded', {
                'ext_id': ext_id, 'name': ext.manifest.name})

        return ext

    def load_from_directory(self, path: str) -> List[Extension]:
        """Scan a directory for extension modules and load them.

        Looks for Python files with Extension subclasses. Each file
        is treated as a potential extension module.

        Args:
            path: Directory path to scan.

        Returns:
            List of loaded Extension instances.
        """
        import os

        loaded = []
        if not os.path.isdir(path):
            logger.debug("Extensions directory not found: %s", path)
            return loaded

        for fname in sorted(os.listdir(path)):
            if not fname.endswith('.py') or fname.startswith('_'):
                continue

            module_name = fname[:-3]  # strip .py
            # Build module path relative to working directory
            rel_path = os.path.relpath(path).replace(os.sep, '.')
            module_path = f"{rel_path}.{module_name}"

            try:
                ext = self.load(module_path)
                loaded.append(ext)
            except Exception as e:
                logger.warning("Failed to load extension from %s: %s",
                               fname, e)

        return loaded

    def enable(self, ext_id: str) -> None:
        """Enable a loaded extension.

        Args:
            ext_id: Extension ID.

        Raises:
            KeyError: If not loaded.
            RuntimeError: If not in LOADED or DISABLED state.
        """
        ext = self._get_extension(ext_id)
        if ext.state not in (ExtensionState.LOADED, ExtensionState.DISABLED):
            raise RuntimeError(
                f"Cannot enable '{ext_id}': state is {ext.state.value}")

        try:
            ext.on_enable()
            ext._state = ExtensionState.ENABLED
            ext._error = None
        except Exception as e:
            ext._state = ExtensionState.ERROR
            ext._error = str(e)
            logger.error("Extension '%s' on_enable failed: %s", ext_id, e)
            raise

        if self._emit:
            self._emit('extension.enabled', {'ext_id': ext_id})

    def disable(self, ext_id: str) -> None:
        """Disable an enabled extension.

        Args:
            ext_id: Extension ID.

        Raises:
            KeyError: If not loaded.
            RuntimeError: If not in ENABLED state.
        """
        ext = self._get_extension(ext_id)
        if ext.state != ExtensionState.ENABLED:
            raise RuntimeError(
                f"Cannot disable '{ext_id}': state is {ext.state.value}")

        try:
            ext.on_disable()
            ext._state = ExtensionState.DISABLED
        except Exception as e:
            ext._state = ExtensionState.ERROR
            ext._error = str(e)
            logger.error("Extension '%s' on_disable failed: %s", ext_id, e)

        if self._emit:
            self._emit('extension.disabled', {'ext_id': ext_id})

    def unload(self, ext_id: str) -> None:
        """Unload an extension completely.

        Disables first if enabled, then calls on_unload().

        Args:
            ext_id: Extension ID.

        Raises:
            KeyError: If not loaded.
        """
        ext = self._get_extension(ext_id)

        if ext.state == ExtensionState.ENABLED:
            try:
                ext.on_disable()
            except Exception as e:
                logger.warning("Extension '%s' on_disable error during unload: %s",
                               ext_id, e)

        try:
            ext.on_unload()
        except Exception as e:
            logger.warning("Extension '%s' on_unload error: %s", ext_id, e)

        ext._state = ExtensionState.UNLOADED

        with self._lock:
            del self._extensions[ext_id]
            self._modules.pop(ext_id, None)

        if self._emit:
            self._emit('extension.unloaded', {'ext_id': ext_id})

    def reload(self, ext_id: str) -> Extension:
        """Hot-reload an extension: unload, re-import, load.

        Args:
            ext_id: Extension ID.

        Returns:
            New Extension instance.

        Raises:
            KeyError: If not loaded or module path unknown.
        """
        with self._lock:
            module_path = self._modules.get(ext_id)
        if not module_path:
            raise KeyError(f"No module path for '{ext_id}'")

        self.unload(ext_id)

        # Force re-import
        if module_path in sys.modules:
            del sys.modules[module_path]

        return self.load(module_path)

    def list_extensions(self) -> List[dict]:
        """Return summary of all loaded extensions."""
        result = []
        for ext_id, ext in self._extensions.items():
            result.append({
                'id': ext_id,
                'name': ext.manifest.name,
                'version': ext.manifest.version,
                'state': ext.state.value,
                'error': ext.error,
                'loaded_at': ext._loaded_at.isoformat() if ext._loaded_at else None,
            })
        return result

    def get(self, ext_id: str) -> Optional[Extension]:
        """Get an extension by ID."""
        return self._extensions.get(ext_id)

    def count(self) -> int:
        """Return number of loaded extensions."""
        return len(self._extensions)

    def _get_extension(self, ext_id: str) -> Extension:
        """Get extension or raise KeyError."""
        if ext_id not in self._extensions:
            raise KeyError(f"Extension '{ext_id}' not loaded")
        return self._extensions[ext_id]

    # ── Lifecycle (for ServiceRegistry) ───────────────────────

    def health(self) -> dict:
        """Health report."""
        state_counts = {}
        for ext in self._extensions.values():
            s = ext.state.value
            state_counts[s] = state_counts.get(s, 0) + 1
        return {
            'status': 'ok',
            'total': len(self._extensions),
            'states': state_counts,
        }
