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
from core.platform.cache import CacheService
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

    # CacheService — unified in-memory + optional disk cache
    registry.register('cache', CacheService, singleton=True)

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

    # CapabilityRouter — resolves AI capability intents to backends
    def _make_capability_router():
        from core.platform.ai_capabilities import CapabilityRouter
        mr, vm = None, None
        try:
            from integrations.agent_engine.model_registry import model_registry
            mr = model_registry
        except ImportError:
            pass
        try:
            from integrations.service_tools.vram_manager import vram_manager
            vm = vram_manager
        except ImportError:
            pass
        return CapabilityRouter(model_registry=mr, vram_manager=vm)

    registry.register('capability_router', _make_capability_router, singleton=True)

    # EnvironmentManager — agent execution environments
    from core.platform.agent_environment import EnvironmentManager
    registry.register('environments',
                       lambda: EnvironmentManager(
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
        # Verify extension signatures before loading (WS14)
        _verify_extension_signatures(extensions_dir)
        ext_reg.load_from_directory(extensions_dir)

    # ── Register Orchestrator Services (lazy, fail-open) ─────

    _register_orchestrator_services(registry)

    # ── PeerLink — P2P communication layer ────────────────────

    try:
        from core.peer_link.link_manager import get_link_manager
        from core.peer_link.telemetry import get_central_connection
        from core.peer_link.message_bus import get_message_bus

        registry.register('peer_link', get_link_manager, singleton=True)
        registry.register('message_bus', get_message_bus, singleton=True)
        registry.register('central_connection', get_central_connection, singleton=True)

        # Start PeerLink services
        link_mgr = registry.get('peer_link')
        link_mgr.start()

        central = registry.get('central_connection')
        central.start()

        logger.debug("PeerLink services registered")
    except Exception as e:
        logger.debug("PeerLink not available: %s", e)

    # ── Connect EventBus to Crossbar WAMP (if configured) ────

    cburl = os.environ.get('CBURL')
    if cburl:
        bus.connect_wamp(cburl, os.environ.get('CBREALM', 'realm1'))

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


def _register_orchestrator_services(registry: ServiceRegistry) -> None:
    """Register agent orchestrator services as lazy platform services.

    These are production services (AgentDaemon, FederatedAggregator) that
    participate in the platform lifecycle. Lazy-loaded so tests and minimal
    environments don't pay the import cost.
    """
    # AgentDaemon — background goal dispatch
    def _make_agent_daemon():
        try:
            from integrations.agent_engine.agent_daemon import agent_daemon
            return agent_daemon
        except ImportError:
            logger.debug("AgentDaemon not available — skipping")
            return None

    try:
        registry.register('agent_daemon', _make_agent_daemon, singleton=True)
    except Exception:
        pass

    # FederatedAggregator — hive learning aggregation
    def _make_federated_aggregator():
        try:
            from integrations.agent_engine.federated_aggregator import (
                get_federated_aggregator,
            )
            return get_federated_aggregator()
        except ImportError:
            logger.debug("FederatedAggregator not available — skipping")
            return None

    try:
        registry.register('federation', _make_federated_aggregator, singleton=True)
    except Exception:
        pass


def _verify_extension_signatures(extensions_dir: str) -> None:
    """Verify extension manifest signatures before loading.

    Logs a warning for unsigned or invalid-signed extensions.
    Does NOT block loading — verification is advisory for now.
    Production environments should set HART_REQUIRE_SIGNED_EXTENSIONS=1
    to enforce signatures.
    """
    require_signed = os.environ.get('HART_REQUIRE_SIGNED_EXTENSIONS', '') == '1'

    for entry in os.listdir(extensions_dir):
        ext_path = os.path.join(extensions_dir, entry)
        if not os.path.isdir(ext_path):
            continue

        manifest_path = os.path.join(ext_path, 'manifest.json')
        sig_path = os.path.join(ext_path, 'manifest.sig')

        if not os.path.isfile(manifest_path):
            continue

        if not os.path.isfile(sig_path):
            if require_signed:
                logger.warning(
                    "Extension '%s' has no signature — skipping (HART_REQUIRE_SIGNED_EXTENSIONS=1)",
                    entry)
                # Remove from directory listing so load_from_directory skips it
                try:
                    os.rename(ext_path, ext_path + '.unsigned')
                except OSError:
                    pass
            else:
                logger.debug("Extension '%s' is unsigned (advisory)", entry)
            continue

        # Verify signature using master public key
        try:
            from security.master_key import verify_release
            import json
            with open(manifest_path, 'rb') as f:
                manifest_bytes = f.read()
            with open(sig_path, 'rb') as f:
                sig_bytes = f.read()

            if not verify_release(manifest_bytes, sig_bytes):
                logger.warning(
                    "Extension '%s' has INVALID signature — %s",
                    entry, "skipping" if require_signed else "loading anyway (advisory)")
                if require_signed:
                    try:
                        os.rename(ext_path, ext_path + '.badsig')
                    except OSError:
                        pass
        except ImportError:
            logger.debug("master_key not available — skipping signature verification")
        except Exception as e:
            logger.warning("Extension '%s' signature check error: %s", entry, e)
