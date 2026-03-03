# Writing HART OS Extensions

Extensions are the plugin system for HART OS. They can register services,
subscribe to events, read/write config, and provide AppManifests.

**Files:** `core/platform/extensions.py`, `core/platform/extension_sandbox.py`

## Extension ABC

Every extension must subclass `Extension` and implement:

```python
from core.platform.extensions import Extension
from core.platform.app_manifest import AppManifest

class MyExtension(Extension):
    @property
    def manifest(self):
        return AppManifest(
            id='my-extension',
            name='My Extension',
            type='extension',
            version='1.0.0',
            entry={'module': 'extensions.my_extension'},
        )

    def on_load(self, registry, config):
        """Called when extension is loaded. Register services here."""
        self._svc = MyService()
        registry.register('my_service', lambda: self._svc)

    def on_enable(self):
        """Called when extension is enabled. Start work here."""
        self._svc.start()

    def on_disable(self):
        """Called when extension is disabled. Stop work here."""
        self._svc.stop()

    def on_unload(self):
        """Called when extension is unloaded. Clean up here."""
        self._svc = None
```

## State Machine

```
UNLOADED -> LOADED -> ENABLED <-> DISABLED -> UNLOADED
              |          |
            ERROR      ERROR
```

- `UNLOADED`: Not yet imported
- `LOADED`: Module imported, `on_load()` called, manifest registered
- `ENABLED`: Active and running (`on_enable()` called)
- `DISABLED`: Temporarily paused (`on_disable()` called)
- `ERROR`: Exception during any lifecycle transition

## Security Requirements

Before import, every extension source file is analyzed by `ExtensionSandbox`
(AST-based static analysis). The following patterns are blocked:

**Blocked function calls:**
- `eval()`, `exec()`, `compile()`, `__import__()`

**Blocked imports:**
- `subprocess`, `ctypes`, `multiprocessing`

**Blocked attribute access:**
- `os.system`, `os.popen`, `subprocess.run`, `subprocess.Popen`
- `subprocess.call`, `subprocess.check_output`, `subprocess.check_call`
- `shutil.rmtree`

Extensions that fail sandbox analysis are rejected before import.

## Permission Declarations

Extensions declare their permission needs via a module-level constant:

```python
EXTENSION_PERMISSIONS = [
    'events.theme.*',     # Subscribe to theme events
    'config.read',        # Read platform config
    'config.write',       # Write platform config
]
```

The sandbox extracts these declarations via AST analysis
(`ExtensionSandbox.check_permission_declarations()`).

## Hot Reload

Extensions can be reloaded without restarting the OS:

```python
from core.platform.extensions import ExtensionRegistry
ext_registry = registry.get('extensions')
ext_registry.reload('my-extension')
```

This calls `on_disable()` -> `on_unload()` -> re-import -> `on_load()` -> `on_enable()`.

## Registration

When an extension is loaded, its manifest is automatically registered in
AppRegistry. This makes it discoverable via search, spotlight, and the
shell manifest API.

## Directory Loading

Place extension modules in the `extensions/` directory. Bootstrap scans
this directory automatically:

```
extensions/
  my_extension.py
  another_ext.py
```

Or load manually:

```python
ext_registry.load_from_directory('/path/to/extensions')
```

## See Also

- [sdk.md](sdk.md) -- HART SDK quick start
- [security.md](security.md) -- Extension sandbox details
- [../features/platform-layer.md](../features/platform-layer.md) -- Platform architecture
