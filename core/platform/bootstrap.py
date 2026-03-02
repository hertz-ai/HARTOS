"""
Platform Bootstrap — One-time initialization that wires all platform services.

Called once at server start (from langchain_gpt_api.py or tests).
Registers core services, migrates existing shell_manifest.py panels,
detects native apps, loads extensions, and starts lifecycle services.

Usage:
    from core.platform.bootstrap import bootstrap_platform
    registry = bootstrap_platform()
    # All services now available:
    #   registry.get('events')     -> EventBus
    #   registry.get('apps')       -> AppRegistry
    #   registry.get('extensions') -> ExtensionRegistry
"""

import logging
import os
import shutil
from typing import Optional

from core.platform.registry import ServiceRegistry, get_registry
from core.platform.events import EventBus
from core.platform.app_manifest import AppManifest, AppType
from core.platform.app_registry import AppRegistry
from core.platform.extensions import ExtensionRegistry

logger = logging.getLogger('hevolve.platform')


def bootstrap_platform(extensions_dir: Optional[str] = None) -> ServiceRegistry:
    """One-time platform initialization. Returns the global registry.

    Idempotent — safe to call multiple times (no-ops if already bootstrapped).

    Args:
        extensions_dir: Optional path to scan for extensions.
                        Defaults to 'extensions/' relative to repo root.

    Returns:
        The global ServiceRegistry with all core services registered.
    """
    registry = get_registry()

    # Guard against double-bootstrap
    if registry.has('events'):
        return registry

    # ── Register Core Platform Services ───────────────────────

    # EventBus — decoupled communication
    registry.register('events', EventBus, singleton=True)
    bus = registry.get('events')

    # AppRegistry — central app catalog (wired to EventBus)
    registry.register('apps', lambda: AppRegistry(event_emitter=bus.emit),
                       singleton=True)
    apps = registry.get('apps')

    # ExtensionRegistry — platform extension lifecycle
    registry.register('extensions',
                       lambda: ExtensionRegistry(
                           service_registry=registry,
                           event_emitter=bus.emit),
                       singleton=True)

    # ── Migrate Existing Shell Manifest ───────────────────────

    _migrate_shell_manifest(apps)

    # ── Detect Native Apps (Rust/C++ binaries) ────────────────

    _register_native_apps(apps)

    # ── Load Extensions ───────────────────────────────────────

    if extensions_dir is None:
        # Default: extensions/ relative to repo root
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        extensions_dir = os.path.join(repo_root, 'extensions')

    ext_reg = registry.get('extensions')
    if os.path.isdir(extensions_dir):
        ext_reg.load_from_directory(extensions_dir)

    # ── Start Lifecycle Services ──────────────────────────────

    registry.start_all()

    total = apps.count()
    ext_count = ext_reg.count()
    logger.info("Platform bootstrapped: %d apps, %d extensions", total, ext_count)

    return registry


def _migrate_shell_manifest(apps: AppRegistry) -> None:
    """Import existing shell_manifest.py panels into AppRegistry.

    Backward compatible — if shell_manifest.py can't be imported
    (e.g., during isolated testing), this gracefully no-ops.
    """
    try:
        from integrations.agent_engine.shell_manifest import (
            PANEL_MANIFEST, SYSTEM_PANELS, DYNAMIC_PANELS,
        )
        panel_count = apps.load_panel_manifest(PANEL_MANIFEST)
        system_count = apps.load_system_panels(SYSTEM_PANELS)

        # Dynamic panels
        dynamic_count = 0
        for panel_id, panel in DYNAMIC_PANELS.items():
            if panel_id not in [m.id for m in apps.list_all()]:
                manifest = AppManifest(
                    id=panel_id,
                    name=panel.get('title', panel_id),
                    version='1.0.0',
                    type=AppType.DYNAMIC_PANEL.value,
                    icon=panel.get('icon', 'open_in_new'),
                    entry={'route': panel.get('route', '')},
                    group=panel.get('group', ''),
                    default_size=tuple(panel.get('default_size', [700, 500])),
                )
                apps.register(manifest)
                dynamic_count += 1

        logger.debug("Migrated %d panels, %d system, %d dynamic from shell_manifest",
                      panel_count, system_count, dynamic_count)

    except ImportError:
        logger.debug("shell_manifest.py not available — skipping migration")
    except Exception as e:
        logger.warning("Shell manifest migration failed: %s", e)


def _register_native_apps(apps: AppRegistry) -> None:
    """Detect and register installed native binaries.

    Uses shutil.which() for binary detection — same pattern as
    service_manager.py EngineService.detect().
    Only registers apps that are actually installed on this system.
    """
    NATIVE_APPS = [
        AppManifest(
            id='rustdesk', name='RustDesk', version='auto',
            type=AppType.DESKTOP_APP.value, icon='desktop_windows',
            entry={'exec': 'rustdesk', 'bridge': 'rustdesk_bridge'},
            group='Remote', platforms=['linux', 'windows', 'macos'],
            permissions=['network', 'display', 'input'],
            description='Open-source remote desktop',
            tags=['remote', 'vnc', 'desktop'],
        ),
        AppManifest(
            id='sunshine', name='Sunshine', version='auto',
            type=AppType.SERVICE.value, icon='wb_sunny',
            entry={'exec': 'sunshine', 'bridge': 'sunshine_bridge',
                   'http': 'https://localhost:47990'},
            group='Remote', platforms=['linux', 'windows'],
            permissions=['network', 'display'],
            description='GPU-accelerated game streaming host',
            tags=['remote', 'streaming', 'gaming'],
        ),
        AppManifest(
            id='moonlight', name='Moonlight', version='auto',
            type=AppType.DESKTOP_APP.value, icon='nightlight',
            entry={'exec': 'moonlight', 'bridge': 'sunshine_bridge'},
            group='Remote', platforms=['linux', 'windows', 'macos'],
            permissions=['network', 'display'],
            description='Low-latency game streaming client',
            tags=['remote', 'streaming', 'gaming'],
        ),
    ]

    detected = 0
    for manifest in NATIVE_APPS:
        exec_name = manifest.entry.get('exec', '')
        if exec_name and shutil.which(exec_name):
            if not apps.get(manifest.id):
                apps.register(manifest)
                detected += 1
                logger.debug("Detected native app: %s", manifest.name)

    if detected:
        logger.info("Detected %d native apps", detected)
