# Platform Layer

**Directory:** `core/platform/`

The durable OS substrate that unifies all HART OS subsystems. Eight core
modules, zero external dependencies added.

## ServiceRegistry (`registry.py`)

Typed, lazy-loaded, thread-safe service container. Replaces the ad-hoc
`_instance = None` + `get_*()` singleton pattern used in 6+ modules.

- **String-named services** -- allows swapping implementations at runtime
- **Lazy instantiation** -- factory not called until first `get()`
- **Singleton by default** -- one instance per name; factory mode available
- **Lifecycle protocol** -- optional `start()/stop()/health()` for managed services
- **Dependency ordering** -- topological sort ensures correct start order
- **Thread-safe** -- all mutations under `threading.Lock`

```python
from core.platform.registry import get_registry
registry = get_registry()
registry.register('theme', ThemeService, singleton=True)
registry.register('compute', ComputeService, depends_on=['events'])
registry.start_all()    # starts in dependency order
registry.health()       # {name: {status, uptime, error}}
registry.stop_all()     # stops in reverse order
```

Global access: `get_registry()` / `reset_registry()`.

## EventBus (`events.py`)

Topic-based pub/sub that decouples subsystems without direct imports.

- **Dot-separated topics**: `config.display.scale`, `theme.changed`
- **Wildcard subscriptions**: `theme.*` matches `theme.changed`, `theme.preset.applied`
- **Sync dispatch** by default (callback in emitter's thread)
- **Optional `async_emit()`** for non-blocking dispatch
- **`once()` subscriptions** -- auto-removed after first call
- **WAMP bridge**: local events optionally publish to Crossbar router; WAMP events fire local callbacks
- **Topic mapping**: `theme.changed` <--> `com.hartos.event.theme.changed`

Module-level helper for use anywhere:

```python
from core.platform.events import emit_event
emit_event('theme.changed', {'name': 'dark'})
```

Auto-connects to WAMP when `CBURL` env var is set during bootstrap.

## PlatformConfig (`config.py`)

3-layer configuration with change notifications.

Resolution order: **env var > override > DB > defaults**.

- **TTL cache** -- avoids repeated DB/env lookups
- **Typed converters** -- `get_int()`, `get_bool()`, `get_float()`
- **Change callbacks** -- `on_change(key, callback)` for reactive config

Generalizes the `compute_config.py` pattern to all platform settings.

## AppManifest (`app_manifest.py`)

Universal manifest for ALL app types in HART OS. Nine `AppType` values:

| Type | Description |
|------|-------------|
| `nunba_panel` | Nunba web panel |
| `system_panel` | Built-in system panel |
| `dynamic_panel` | Dynamically registered panel |
| `desktop_app` | Native desktop application |
| `service` | Background service |
| `agent` | AI agent |
| `mcp_server` | MCP tool server |
| `channel` | Channel adapter |
| `extension` | Platform extension |

Backward compatibility: `from_panel_manifest()` / `from_system_panel()`.
Search support: `matches_search(query)` for spotlight.

## AppRegistry (`app_registry.py`)

Central app catalog. All apps (panels, services, agents, extensions) live here.

- `register(manifest)` / `unregister(app_id)`
- `get(app_id)` / `list_all()` / `list_by_type(type)` / `list_by_group(group)`
- `search(query)` -- fuzzy search across name, description, tags
- `load_panel_manifest()` / `load_system_panels()` -- bulk import from legacy data
- `to_shell_manifest()` -- backward compat with shell_manifest.py

Emits `app.registered` and `app.unregistered` events.

## ManifestValidator (`manifest_validator.py`)

OS-level contracts for AppManifest integrity. Every manifest must pass before
registration. Static methods, fail-closed, clear error reasons.

Validates:
- **ID**: alphanumeric/hyphens/underscores, 1-64 chars
- **Type**: valid AppType enum value
- **Version**: semver X.Y.Z or `'auto'`
- **Entry**: required keys per AppType (defined in `ENTRY_SCHEMA`)
- **Permissions**: must be in `KNOWN_PERMISSIONS` (16 allowed)
- **AI Capabilities**: valid type, no NaN/Inf, accuracy 0-1
- **Size**: positive integers, max 7680x4320

```python
from core.platform.manifest_validator import ManifestValidator
valid, errors = ManifestValidator.validate(manifest)
```

## ExtensionSandbox (`extension_sandbox.py`)

AST-based static analysis that runs BEFORE `importlib.import_module()`.
Zero external dependencies -- stdlib `ast` + `hashlib` only.

Blocked patterns:
- **Calls**: `eval()`, `exec()`, `compile()`, `__import__()`
- **Imports**: `subprocess`, `ctypes`, `multiprocessing`
- **Attributes**: `os.system`, `os.popen`, `subprocess.run`, `shutil.rmtree`, etc.

Also provides `verify_signature()` (Ed25519) and `check_permission_declarations()`.

## ExtensionRegistry (`extensions.py`)

Platform-wide plugin system. State machine:

```
UNLOADED -> LOADED -> ENABLED <-> DISABLED -> UNLOADED
              |          |
            ERROR      ERROR
```

- `load(module_path)` -- import + AST sandbox check + on_load()
- `enable(ext_id)` / `disable(ext_id)`
- `reload(ext_id)` -- hot-reload without OS restart
- `load_from_directory(path)` -- scan and load all extensions

Extensions auto-register in AppRegistry via their manifest.

## Bootstrap (`bootstrap.py`)

`bootstrap_platform()` -- one-time initialization, idempotent.

1. Registers core services (EventBus, AppRegistry, ExtensionRegistry, etc.)
2. Migrates 55 shell_manifest panels (31 Nunba + 10 system + 14 dynamic)
3. Detects native apps (RustDesk, Sunshine, Moonlight via `shutil.which`)
4. Loads extensions from `extensions/` directory
5. Connects WAMP bridge if `CBURL` env var is set

```python
from core.platform.bootstrap import bootstrap_platform
registry = bootstrap_platform()
```

## See Also

- [ai-capabilities.md](ai-capabilities.md) -- AI capability intents
- [agent-environments.md](agent-environments.md) -- Agent execution scopes
- [../developer/extensions.md](../developer/extensions.md) -- Writing extensions
